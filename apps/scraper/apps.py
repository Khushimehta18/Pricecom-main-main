from django.apps import AppConfig

class ScraperConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.scraper'

    def ready(self):
        # Hooks or signal registration would go here.
        pass
