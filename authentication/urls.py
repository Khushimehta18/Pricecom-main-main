from django.urls import path
from . import views

# This app previously exposed its own login/register routes which conflicted
# with django-allauth. We leave only the activation endpoint here (if any
# processes rely on it). All standard auth routes are served by allauth
# via `path('accounts/', include('allauth.urls'))` in the project urls.

urlpatterns = [
    path('activate/<uidb64>/<token>/', views.activate, name='activate'),
]
