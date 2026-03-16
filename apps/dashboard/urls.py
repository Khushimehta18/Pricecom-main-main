from django.urls import path
from . import views
from apps.scraper.views import set_target_price

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_home, name='index'),
    path('watchlist/', views.dashboard_watchlist, name='watchlist'),
    path('watchlist/remove/<uuid:uuid>/', views.watchlist_remove, name='watchlist_remove'),
    path('watchlist/update-target/<uuid:uuid>/', views.watchlist_update_target, name='watchlist_update_target'),
    path('alerts/', views.dashboard_alerts, name='alerts'),
    path('alerts/set/', views.set_price_alert_from_product, name='set_price_alert_from_product'),
    path('api/products/', views.api_products, name='api_products'),
    path('api/products/<int:uuid>/history/', views.api_product_history, name='api_product_history'),
    path('api/watchlist/', views.api_watchlist, name='api_watchlist'),
    path('api/watchlist/set-target/', set_target_price, name='set_target_price'),
    path('api/system-health/', views.api_system_health, name='api_system_health'),
    path('api/search/', views.api_search, name='api_search'),
    path('api/image-search/', views.api_image_search, name='api_image_search'),
    path('api/result/<str:task_id>/', views.api_result, name='api_result'),
]
