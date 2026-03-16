from django.urls import path
from . import watchlist_api

app_name = 'watchlist'

urlpatterns = [
    path('add', watchlist_api.add_to_watchlist, name='add_to_watchlist'),
    path('', watchlist_api.get_watchlist, name='get_watchlist'),
    path('remove/<str:product_id>', watchlist_api.remove_from_watchlist, name='remove_from_watchlist'),
]
