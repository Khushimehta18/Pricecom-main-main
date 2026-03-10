from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_home, name='home'),
    path('api/products/', views.api_products, name='api_products'),
    path('api/products/<int:uuid>/history/', views.api_product_history, name='api_product_history'),
    path('api/watchlist/', views.api_watchlist, name='api_watchlist'),
    path('api/system-health/', views.api_system_health, name='api_system_health'),
    path('api/search/', views.api_search, name='api_search'),
    path('api/image-search/', views.api_image_search, name='api_image_search'),
    path('api/result/<str:task_id>/', views.api_result, name='api_result'),
]
