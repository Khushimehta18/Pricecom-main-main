from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView
from apps.dashboard import views as dashboard_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Path is 'api/' exactly as requested.
    # This means accounts urls will be appended to 'api/'.
    # Include the local accounts app first so its custom views/templates take precedence
    path('accounts/', include('apps.accounts.urls')),
    path('accounts/', include('authentication.urls')), 
    path('scraper/', include('apps.scraper.urls')),
    # Direct mappings for image-search endpoints (ensure availability)
    path('dashboard/api/image-search/', dashboard_views.api_image_search),
    path('dashboard/api/result/<str:task_id>/', dashboard_views.api_result),
    path('dashboard/', include('apps.dashboard.urls')),
    # Social Auth
    # Allauth kept after local accounts so local routes win for same paths
    path('accounts/', include('allauth.urls')),
    path('', TemplateView.as_view(template_name='home.html'), name='home'),
]
