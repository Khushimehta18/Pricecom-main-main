import os
import logging
from django.apps import AppConfig
from django.conf import settings as dj_settings

class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    # The Config Registry: Necessary for nested autodiscovery
    name = 'apps.accounts' 

    def ready(self):
        """
        Task 1: App Config
        Register signals when app is ready.
        """
        # Import signals to ensure they are registered when the app is ready.
        try:
            from . import signals  # noqa: F401
        except Exception:
            # If signals fail to import, log at debug level to avoid breaking startup
            logging.getLogger(__name__).debug('apps.accounts.signals import failed', exc_info=True)

        # Auto-create or update Google SocialApp if env vars are provided
        try:
            # Only attempt when socialaccount is installed and migrations/db are available
            if 'allauth.socialaccount' in dj_settings.INSTALLED_APPS and 'django.contrib.sites' in dj_settings.INSTALLED_APPS:
                from django.contrib.sites.models import Site
                from allauth.socialaccount.models import SocialApp

                client_id = os.getenv('SOCIAL_GOOGLE_CLIENT_ID') or os.getenv('GOOGLE_CLIENT_ID')
                client_secret = os.getenv('SOCIAL_GOOGLE_SECRET') or os.getenv('GOOGLE_CLIENT_SECRET')
                if client_id and client_secret:
                    site = None
                    try:
                        site = Site.objects.get(id=getattr(dj_settings, 'SITE_ID', 1))
                    except Exception:
                        site = None

                    if site is not None:
                        app, created = SocialApp.objects.get_or_create(provider='google', name='Google', defaults={'client_id': client_id, 'secret': client_secret})
                        updated = False
                        if not created:
                            if app.client_id != client_id:
                                app.client_id = client_id
                                updated = True
                            if app.secret != client_secret:
                                app.secret = client_secret
                                updated = True
                            if updated:
                                app.save()
                        # Ensure site association
                        if site not in app.sites.all():
                            app.sites.add(site)
        except Exception:
            logging.getLogger(__name__).debug('SocialApp auto-creation skipped or failed', exc_info=True)
