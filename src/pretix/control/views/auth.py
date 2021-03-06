import logging
import time
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate, login as auth_login, logout as auth_logout,
)
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.http import is_safe_url
from django.utils.translation import ugettext_lazy as _
from django.views.generic import TemplateView
from django_otp import match_token
from u2flib_server import u2f
from u2flib_server.jsapi import DeviceRegistration
from u2flib_server.utils import rand_bytes

from pretix.base.forms.auth import (
    LoginForm, PasswordForgotForm, PasswordRecoverForm, RegistrationForm,
)
from pretix.base.models import TeamInvite, U2FDevice, User
from pretix.base.services.mail import SendMailException, mail
from pretix.helpers.urls import build_absolute_uri

logger = logging.getLogger(__name__)


def login(request):
    """
    Render and process a most basic login form. Takes an URL as GET
    parameter "next" for redirection after successful login
    """
    ctx = {}
    if request.user.is_authenticated:
        return redirect(request.GET.get("next", 'control:index'))
    if request.method == 'POST':
        form = LoginForm(data=request.POST)
        if form.is_valid() and form.user_cache:
            request.session['pretix_auth_long_session'] = (
                settings.PRETIX_LONG_SESSIONS and form.cleaned_data.get('keep_logged_in', False)
            )
            if form.user_cache.require_2fa:
                request.session['pretix_auth_2fa_user'] = form.user_cache.pk
                request.session['pretix_auth_2fa_time'] = str(int(time.time()))
                twofa_url = reverse('control:auth.login.2fa')
                if 'next' in request.GET:
                    twofa_url += '?next=' + quote(request.GET.get('next'))
                return redirect(twofa_url)
            else:
                auth_login(request, form.user_cache)
                request.session['pretix_auth_login_time'] = int(time.time())
                if "next" in request.GET and is_safe_url(request.GET.get("next")):
                    return redirect(request.GET.get("next"))
                return redirect(reverse('control:index'))
    else:
        form = LoginForm()
    ctx['form'] = form
    ctx['can_register'] = settings.PRETIX_REGISTRATION
    ctx['can_reset'] = settings.PRETIX_PASSWORD_RESET
    return render(request, 'pretixcontrol/auth/login.html', ctx)


def logout(request):
    """
    Log the user out of the current session, then redirect to login page.
    """
    auth_logout(request)
    request.session['pretix_auth_login_time'] = 0
    return redirect('control:auth.login')


def register(request):
    """
    Render and process a basic registration form.
    """
    if not settings.PRETIX_REGISTRATION:
        raise PermissionDenied('Registration is disabled')
    ctx = {}
    if request.user.is_authenticated:
        return redirect(request.GET.get("next", 'control:index'))
    if request.method == 'POST':
        form = RegistrationForm(data=request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                form.cleaned_data['email'], form.cleaned_data['password'],
                locale=request.LANGUAGE_CODE,
                timezone=request.timezone if hasattr(request, 'timezone') else settings.TIME_ZONE
            )
            user = authenticate(email=user.email, password=form.cleaned_data['password'])
            user.log_action('pretix.control.auth.user.created', user=user)
            auth_login(request, user)
            request.session['pretix_auth_login_time'] = int(time.time())
            request.session['pretix_auth_long_session'] = (
                settings.PRETIX_LONG_SESSIONS and form.cleaned_data.get('keep_logged_in', False)
            )
            return redirect('control:index')
    else:
        form = RegistrationForm()
    ctx['form'] = form
    return render(request, 'pretixcontrol/auth/register.html', ctx)


def invite(request, token):
    """
    Registration form in case of an invite
    """
    ctx = {}

    try:
        inv = TeamInvite.objects.get(token=token)
    except TeamInvite.DoesNotExist:
        messages.error(request, _('You used an invalid link. Please copy the link from your email to the address bar '
                                  'and make sure it is correct and that the link has not been used before.'))
        return redirect('control:auth.login')

    if request.user.is_authenticated:
        if inv.team.members.filter(pk=request.user.pk).exists():
            messages.error(request, _('You cannot accept the invitation for "{}" as you already are part of '
                                      'this team.').format(inv.team.name))
            return redirect('control:index')
        else:
            with transaction.atomic():
                inv.team.members.add(request.user)
                inv.team.log_action(
                    'pretix.team.member.joined', data={
                        'email': request.user.email,
                        'invite_email': inv.email,
                        'user': request.user.pk
                    }
                )
                inv.delete()
            messages.success(request, _('You are now part of the team "{}".').format(inv.team.name))
            return redirect('control:index')

    if request.method == 'POST':
        form = RegistrationForm(data=request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                form.cleaned_data['email'], form.cleaned_data['password'],
                locale=request.LANGUAGE_CODE,
                timezone=request.timezone if hasattr(request, 'timezone') else settings.TIME_ZONE
            )
            user = authenticate(email=user.email, password=form.cleaned_data['password'])
            user.log_action('pretix.control.auth.user.created', user=user)
            auth_login(request, user)
            request.session['pretix_auth_login_time'] = int(time.time())
            request.session['pretix_auth_long_session'] = (
                settings.PRETIX_LONG_SESSIONS and form.cleaned_data.get('keep_logged_in', False)
            )

            with transaction.atomic():
                inv.team.members.add(request.user)
                inv.team.log_action(
                    'pretix.team.member.joined', data={
                        'email': user.email,
                        'invite_email': inv.email,
                        'user': user.pk
                    }
                )
                inv.delete()
            messages.success(request, _('Welcome to pretix! You are now part of the team "{}".').format(inv.team.name))
            return redirect('control:index')
    else:
        form = RegistrationForm(initial={'email': inv.email})
    ctx['form'] = form
    return render(request, 'pretixcontrol/auth/invite.html', ctx)


class Forgot(TemplateView):
    template_name = 'pretixcontrol/auth/forgot.html'

    def dispatch(self, request, *args, **kwargs):
        if not settings.PRETIX_PASSWORD_RESET:
            raise PermissionDenied('Password reset is disabled')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(request.GET.get("next", 'control:index'))
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if self.form.is_valid():
            user = self.form.cleaned_data['user']

            if settings.HAS_REDIS:
                from django_redis import get_redis_connection
                rc = get_redis_connection("redis")
                if rc.exists('pretix_pwreset_%s' % (user.id)):
                    user.log_action('pretix.control.auth.user.forgot_password.denied.repeated')
                    messages.error(request, _('We already sent you an email in the last 24 hours.'))
                    return redirect('control:auth.forgot')
                else:
                    rc.setex('pretix_pwreset_%s' % (user.id), 3600 * 24, '1')

            try:
                mail(
                    user.email, _('Password recovery'), 'pretixcontrol/email/forgot.txt',
                    {
                        'user': user,
                        'url': (build_absolute_uri('control:auth.forgot.recover')
                                + '?id=%d&token=%s' % (user.id, default_token_generator.make_token(user)))
                    },
                    None, locale=user.locale
                )
            except SendMailException:
                messages.error(request, _('There was an error sending the mail. Please try again later.'))
                return self.get(request, *args, **kwargs)

            user.log_action('pretix.control.auth.user.forgot_password.mail_sent')
            messages.success(request, _('We sent you an e-mail containing further instructions.'))
            return redirect('control:auth.forgot')
        else:
            return self.get(request, *args, **kwargs)

    @cached_property
    def form(self):
        return PasswordForgotForm(data=self.request.POST if self.request.method == 'POST' else None)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = self.form
        return context


class Recover(TemplateView):
    template_name = 'pretixcontrol/auth/recover.html'

    error_messages = {
        'invalid': _('You clicked on an invalid link. Please check that you copied the full '
                     'web address into your address bar. Please note that the link is only valid '
                     'for three days and that the link can only be used once.'),
        'unknownuser': _('We were unable to find the user you requested a new password for.')
    }

    def dispatch(self, request, *args, **kwargs):
        if not settings.PRETIX_PASSWORD_RESET:
            raise PermissionDenied('Password reset is disabled')
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect(request.GET.get("next", 'control:index'))
        try:
            user = User.objects.get(id=self.request.GET.get('id'))
        except User.DoesNotExist:
            return self.invalid('unknownuser')
        if not default_token_generator.check_token(user, self.request.GET.get('token')):
            return self.invalid('invalid')
        return super().get(request, *args, **kwargs)

    def invalid(self, msg):
        messages.error(self.request, self.error_messages[msg])
        return redirect('control:auth.forgot')

    def post(self, request, *args, **kwargs):
        if self.form.is_valid():
            try:
                user = User.objects.get(id=self.request.GET.get('id'))
            except User.DoesNotExist:
                return self.invalid('unknownuser')
            if not default_token_generator.check_token(user, self.request.GET.get('token')):
                return self.invalid('invalid')
            user.set_password(self.form.cleaned_data['password'])
            user.save()
            messages.success(request, _('You can now login using your new password.'))
            user.log_action('pretix.control.auth.user.forgot_password.recovered')
            return redirect('control:auth.login')
        else:
            return self.get(request, *args, **kwargs)

    @cached_property
    def form(self):
        return PasswordRecoverForm(data=self.request.POST if self.request.method == 'POST' else None,
                                   user_id=self.request.GET.get('id'))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = self.form
        return context


def get_u2f_appid(request):
    return '%s://%s' % ('https' if request.is_secure() else 'http', request.get_host())


class Login2FAView(TemplateView):
    template_name = 'pretixcontrol/auth/login_2fa.html'

    @property
    def app_id(self):
        return get_u2f_appid(self.request)

    def dispatch(self, request, *args, **kwargs):
        fail = False
        if 'pretix_auth_2fa_user' not in request.session:
            fail = True
        else:
            try:
                self.user = User.objects.get(pk=request.session['pretix_auth_2fa_user'], is_active=True)
            except User.DoesNotExist:
                fail = True
        logintime = int(request.session.get('pretix_auth_2fa_time', '1'))
        if time.time() - logintime > 300:
            fail = True
        if fail:
            messages.error(request, _('Please try again.'))
            return redirect('control:auth.login')
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        token = request.POST.get('token', '').strip().replace(' ', '')

        valid = False
        if '_u2f_challenge' in self.request.session and token.startswith('{'):
            devices = [DeviceRegistration.wrap(device.json_data)
                       for device in U2FDevice.objects.filter(confirmed=True, user=self.user)]
            challenge = self.request.session.pop('_u2f_challenge')
            try:
                u2f.verify_authenticate(devices, challenge, token, [self.app_id])
                valid = True
            except Exception:
                logger.exception('U2F login failed')
        else:
            valid = match_token(self.user, token)

        if valid:
            auth_login(request, self.user)
            request.session['pretix_auth_login_time'] = int(time.time())
            del request.session['pretix_auth_2fa_user']
            del request.session['pretix_auth_2fa_time']
            if "next" in request.GET and is_safe_url(request.GET.get("next")):
                return redirect(request.GET.get("next"))
            return redirect(reverse('control:index'))
        else:
            messages.error(request, _('Invalid code, please try again.'))
            return redirect('control:auth.login.2fa')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data()

        devices = [DeviceRegistration.wrap(device.json_data)
                   for device in U2FDevice.objects.filter(confirmed=True, user=self.user)]
        if devices:
            challenge = u2f.start_authenticate(devices, challenge=rand_bytes(32))
            self.request.session['_u2f_challenge'] = challenge.json
            ctx['jsondata'] = challenge.json
        else:
            if '_u2f_challenge' in self.request.session:
                del self.request.session['_u2f_challenge']
            ctx['jsondata'] = None

        return ctx

    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
