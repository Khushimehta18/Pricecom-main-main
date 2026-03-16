from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
import json
import threading
import uuid
import time
import os
import logging
from django.conf import settings
from .tasks import image_search_task
from core.services.query_cleaner import normalize_query
from apps.scraper.services.services import ScraperService
from django.db.models import Avg, Prefetch
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from apps.scraper.models import (
    NotificationLog,
    PriceAlert,
    PriceHistory,
    Product,
    StorePrice,
    Watchlist,
)



logger = logging.getLogger(__name__)


def _format_rupees(value: Optional[Decimal]) -> str:
    if value is None:
        return "N/A"
    try:
        return f"₹{int(Decimal(value)):,}"
    except (ValueError, ArithmeticError):
        return "N/A"


def _stat_cards_payload() -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    today = now.date()

    total_products = Product.objects.filter(is_active=True).count()
    refreshed_products = (
        StorePrice.objects.filter(last_updated__gte=week_ago)
        .values_list('product_id', flat=True)
        .distinct()
        .count()
    )

    drops_today_qs = PriceHistory.objects.filter(recorded_at__date=today)
    downward_moves = drops_today_qs.filter(trend='DOWN').count()
    significant_drops = drops_today_qs.filter(is_significant_drop=True).count()

    avg_market_change = drops_today_qs.filter(change_percentage__isnull=False).aggregate(
        avg=Avg('change_percentage')
    )['avg']
    if avg_market_change is None:
        avg_market_change = (
            PriceHistory.objects.filter(recorded_at__gte=week_ago)
            .aggregate(avg=Avg('change_percentage'))
            .get('avg')
        )
    avg_market_change = float(avg_market_change or 0.0)

    open_alerts = PriceAlert.objects.filter(is_triggered=False).count()
    alerts_triggered_today = PriceAlert.objects.filter(
        is_triggered=True, created_at__date=today
    ).count()

    latest_sync = StorePrice.objects.order_by('-last_updated').first()
    freshness_value = 'OFFLINE'
    freshness_meta = 'no sync recorded'
    latency_seconds = None
    if latest_sync and latest_sync.last_updated:
        latency_delta = now - latest_sync.last_updated
        latency_seconds = latency_delta.total_seconds()
        if latency_seconds < 60:
            freshness_value = 'ONLINE'
            freshness_meta = f"{int(latency_seconds)}s ago"
        elif latency_seconds < 3600:
            freshness_value = 'SLOW'
            freshness_meta = f"{int(latency_seconds // 60)}m ago"
        else:
            freshness_value = 'STALE'
            freshness_meta = f"{int(latency_seconds // 3600)}h ago"

    # Build stat cards and meta consistently (use total_products variable)
    stat_cards = [
        {
            'title': 'Products Tracked',
            'value': f"{total_products:,}",
            'sublabel': f"{refreshed_products} refreshed 7d",
            'type': None,
        },
        {
            'title': 'Price Drops Today',
            """
            HTMX endpoint returning table rows for products.
            """
            'value': f"{downward_moves:,}",
            'sublabel': f"{significant_drops} deep cuts",
            'type': None,
        },
        {
            'title': 'Avg Market Change',
            'value': f"{avg_market_change:+.1f}%",
            'sublabel': '7d blended delta',
            'type': None,
        },
        {
            'title': 'Active Alerts',
            'value': f"{open_alerts:,}",
            'sublabel': f"{alerts_triggered_today} fired today",
            'type': None,
        },
        {
            'title': 'Data Freshness',
            'value': freshness_value,
            'sublabel': freshness_meta,
            'type': 'freshness',
        },
    ]

    meta = {
        'avg_market_change': avg_market_change,
        'drops_today': downward_moves,
        'significant_drops': significant_drops,
        'active_alerts': open_alerts,
        'latency_seconds': latency_seconds or 0,
    }

    return stat_cards, meta


def _prediction_payload(meta: Dict[str, Any]) -> Dict[str, Any]:
    drops_today = meta.get('drops_today', 0)
    avg_market_change = meta.get('avg_market_change', 0.0)
    significant = meta.get('significant_drops', 0)

    if drops_today >= 15 or avg_market_change < -2.5:
        signal = 'ACQUIRE WINDOW'
    elif avg_market_change > 1.5:
        signal = 'WAIT 3 DAYS'
    else:
        signal = 'TRACK CLOSELY'

    confidence_base = 70 + (drops_today // 3) - int(abs(avg_market_change))
    confidence = max(55, min(95, confidence_base))

    heuristic_drop_probability = min(99, drops_today * 4 + significant * 3)
    smart_index = max(35, min(95, 80 - int(avg_market_change * 5)))

    signals = [
        f"{drops_today} downward events detected today",
        f"Avg movement {avg_market_change:+.1f}%",
        f"{significant} high-impact swings flagged" if significant else 'Liquidity stable across nodes',
    ]

    return {
        'signal': signal,
        'confidence': confidence,
        'signals': signals,
        'drop_probability': heuristic_drop_probability,
        'smart_index': smart_index,
    }


def _chart_payload(product_id: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
    def fallback() -> Dict[str, Any]:
        return {
            'series': [
                {'name': 'Amazon', 'data': [0] * 7},
                {'name': 'Flipkart', 'data': [0] * 7},
            ],
            'categories': [''] * 7,
            'product_id': None,
        }

    base_qs = StorePrice.objects.select_related('product').prefetch_related(
        Prefetch('history', queryset=PriceHistory.objects.order_by('-recorded_at'))
    )

    if product_id:
        store_prices = list(base_qs.filter(product_id=product_id))
    else:
        seed_history = PriceHistory.objects.select_related('store_price__product').order_by('-recorded_at').first()
        if not seed_history:
            return fallback()
        product_id = seed_history.store_price.product_id
        store_prices = list(base_qs.filter(product_id=product_id))

    if not store_prices:
        return fallback()

    timestamp_set = set()
    store_price_map: Dict[str, Dict[str, float]] = {}

    for store_price in store_prices:
        history_entries = list(store_price.history.all()[:limit])
        if not history_entries:
            continue
        entry_map: Dict[str, float] = {}
        for entry in reversed(history_entries):
            key = entry.recorded_at.isoformat()
            timestamp_set.add(entry.recorded_at)
            entry_map[key] = float(entry.price)
        store_price_map[store_price.store_name] = entry_map

    if not store_price_map:
        return fallback()

    ordered_timestamps = sorted(timestamp_set)[-limit:]
    if not ordered_timestamps:
        return fallback()
    categories = [ts.strftime('%d %b %H:%M') for ts in ordered_timestamps]

    series_payload = []
    for store_name, entry_map in store_price_map.items():
        data_points = []
        last_value = None
        for ts in ordered_timestamps:
            key = ts.isoformat()
            value = entry_map.get(key)
            if value is None:
                value = last_value
            else:
                last_value = value
            data_points.append(round(value, 2) if value is not None else None)
        if any(point is not None for point in data_points):
            series_payload.append({'name': store_name, 'data': data_points})

    if not series_payload:
        return fallback()

    return {
        'series': series_payload,
        'categories': categories,
        'product_id': product_id,
    }


def _watchlist_items(user) -> List[Dict[str, Any]]:
    qs = Watchlist.objects.select_related('product').prefetch_related(
        Prefetch('product__prices', queryset=StorePrice.objects.order_by('current_price'))
    )

    if user and user.is_authenticated:
        qs = qs.filter(user=user)
    else:
        qs = qs.order_by('-created_at')

    items: List[Dict[str, Any]] = []
    for entry in qs[:3]:
        product = entry.product
        if not product:
            continue

        current_price = product.current_lowest_price
        if current_price is None:
            current_price = (
                product.prices.all().order_by('current_price').values_list('current_price', flat=True).first()
            )

        target_price = entry.target_price or (
            Decimal(current_price) * Decimal('0.97') if current_price else None
        )

        if current_price is None:
            continue

        delta_value = Decimal(current_price) - Decimal(target_price or current_price)
        progress = 0
        if target_price and current_price:
            try:
                ratio = Decimal(target_price) / Decimal(current_price)
                progress = int(max(0, min(100, ratio * 100)))
            except (ArithmeticError, ValueError):
                progress = 0

        magnitude = abs(delta_value)
        if magnitude >= 1000:
            magnitude_label = f"{magnitude / Decimal('1000'):.1f}K"
        else:
            magnitude_label = f"{magnitude:.0f}"

        items.append(
            {
                'name': product.name[:22].upper(),
                'target': _format_rupees(target_price),
                'current': _format_rupees(current_price),
                'delta': f"{'+' if delta_value >= 0 else '-'}{magnitude_label}_DELTA",
                'pct': progress or 5,
            }
        )

    if items:
        return items

    # Fallback to most recently updated products
    fallback_products = (
        Product.objects.prefetch_related('prices').filter(is_active=True).order_by('-updated_at')[:3]
    )
    for product in fallback_products:
        current_price = product.current_lowest_price
        if current_price is None:
            current_price = (
                product.prices.all().order_by('current_price').values_list('current_price', flat=True).first()
            )
        if current_price is None:
            continue
        target_price = Decimal(current_price) * Decimal('0.97')
        items.append(
            {
                'name': product.name[:22].upper(),
                'target': _format_rupees(target_price),
                'current': _format_rupees(current_price),
                'delta': '+0_DELTA',
                'pct': 65,
            }
        )

    return items


def _system_health_snapshot() -> Dict[str, Any]:
    now = timezone.now()
    total_nodes = StorePrice.objects.count()
    active_nodes = StorePrice.objects.filter(is_available=True).count()
    events_last_minute = PriceHistory.objects.filter(
        recorded_at__gte=now - timedelta(minutes=1)
    ).count()

    per_second = max(1, round(events_last_minute / 60, 1))
    latest_sync = StorePrice.objects.order_by('-last_updated').values_list('last_updated', flat=True).first()
    latency_display = 'n/a'
    if latest_sync:
        latency_delta = now - latest_sync
        if latency_delta.total_seconds() < 1:
            latency_display = f"{int(latency_delta.total_seconds() * 1000)}ms"
        elif latency_delta.total_seconds() < 60:
            latency_display = f"{latency_delta.total_seconds():.1f}s"
        else:
            latency_display = f"{int(latency_delta.total_seconds() // 60)}m"

    cpu_load = min(97, max(20, int(per_second * 8)))

    nodes_display = f"{active_nodes}/{total_nodes or 1}"
    return {
        'nodes': nodes_display,
        'queue': f"{per_second}/s",
        'latency': latency_display,
        'cpu': cpu_load,
    }

@login_required
def dashboard_home(request):
    """
    Dashboard Home View -- hydrates template context with live metrics.
    """

    stat_cards, meta = _stat_cards_payload()
    prediction_payload = _prediction_payload(meta)
    chart_seed = _chart_payload()

    context = {
        'stat_cards': stat_cards,
        'active_alerts': meta['active_alerts'],
        'prediction': prediction_payload,
        'chart_seed': chart_seed,
    }
    return render(request, 'dashboard/index.html', context)

SORT_CHOICES = [
    ('newest', 'Newest additions'),
    ('lowest_price', 'Lowest current price'),
    ('biggest_drop', 'Biggest price drop'),
    ('highest_discount', 'Highest discount'),
]

AVAILABILITY_CHOICES = [
    ('any', 'All availability'),
    ('in_stock', 'In stock'),
    ('out_of_stock', 'Out of stock'),
]


def _map_watchlist_trend(raw_indicator: Optional[str]) -> str:
    normalized = (raw_indicator or '').upper()
    if any(token in normalized for token in ('DOWN', 'BEAR', 'DROP')):
        return 'Dropping'
    if any(token in normalized for token in ('UP', 'BULL', 'RISE')):
        return 'Rising'
    return 'Stable'


def _build_watchlist_payload(user) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    watchlist_qs = (
        Watchlist.objects.filter(user=user)
        .select_related('product')
        .prefetch_related(
            Prefetch('product__prices', queryset=StorePrice.objects.order_by('current_price'))
        )
    )

    for entry in watchlist_qs:
        product = entry.product
        store_prices = [sp for sp in product.prices.all() if sp.current_price is not None]
        if not store_prices:
            continue

        store_prices.sort(key=lambda sp: sp.current_price or Decimal('0'))
        best_store = store_prices[0]
        best_price_value = best_store.current_price or Decimal('0')

        entry_price_value = (
            entry.last_notified_price
            or product.current_lowest_price
            or product.base_price
            or best_price_value
        )
        initial_price_value = entry.added_price or product.base_price or best_price_value

        price_difference_value = Decimal('0')
        if entry_price_value is not None and best_price_value is not None:
            price_difference_value = entry_price_value - best_price_value

        price_drop_value = price_difference_value if price_difference_value > 0 else Decimal('0')
        price_drop_percent = 0.0
        if price_drop_value > 0 and entry_price_value:
            try:
                price_drop_percent = float((price_drop_value / entry_price_value) * Decimal('100'))
            except (InvalidOperation, ZeroDivisionError):
                price_drop_percent = 0.0

        discount_percent = float(product.discount_percentage or Decimal('0'))
        trend_label = _map_watchlist_trend(product.trend_indicator)
        trend_class = (
            'text-brand-success' if trend_label == 'Dropping'
            else 'text-brand-accent' if trend_label == 'Rising'
            else 'text-brand-textMuted'
        )

        metadata = product.metadata if isinstance(product.metadata, dict) else {}
        image_url = (
            best_store.image_url
            or metadata.get('image_url')
            or metadata.get('thumbnail')
        )

        availability_key = 'in_stock' if best_store.is_available else 'out_of_stock'
        availability_label = 'In Stock' if best_store.is_available else 'Out of Stock'
        last_updated = best_store.last_updated or product.updated_at

        store_breakdown = []
        for sp in store_prices:
            price_value = sp.current_price or Decimal('0')
            store_breakdown.append({
                'name': sp.store_name,
                'price_display': _format_rupees(price_value),
                'price_value': price_value,
                'is_best': sp == best_store,
            })

        store_count = len(store_prices)
        target_reached = bool(entry.target_price and best_price_value <= entry.target_price) if entry.target_price else False
        baseline_drop_percent = 0.0
        if initial_price_value and initial_price_value > 0 and best_price_value:
            try:
                baseline_drop_percent = float(((initial_price_value - best_price_value) / initial_price_value) * Decimal('100'))
            except (InvalidOperation, ZeroDivisionError):
                baseline_drop_percent = 0.0

        payload.append({
            'uuid': entry.uuid,
            'product_name': product.name,
            'product_image': image_url,
            'store_name': best_store.store_name,
            'product_url': best_store.product_url,
            'availability_label': availability_label,
            'availability_key': availability_key,
            'trend_label': trend_label,
            'trend_class': trend_class,
            'current_price_display': _format_rupees(best_price_value),
            'current_price_value': best_price_value,
            'best_store_name': best_store.store_name,
            'best_store_url': best_store.product_url,
            'store_count': store_count,
            'store_breakdown': store_breakdown,
            'entry_price_display': _format_rupees(entry_price_value) if entry_price_value else '—',
            'price_difference_display': _format_rupees(abs(price_difference_value)),
            'price_difference_value': abs(price_difference_value),
            'price_drop_percent': round(price_drop_percent, 1),
            'discount_percent': round(discount_percent, 1),
            'baseline_price_display': _format_rupees(initial_price_value) if initial_price_value else '—',
            'baseline_drop_percent': round(baseline_drop_percent, 1),
            'last_updated': last_updated,
            'target_price_value': entry.target_price,
            'target_price_display': _format_rupees(entry.target_price) if entry.target_price else '—',
            'created_at': entry.created_at,
            'price_drop_value': price_drop_value,
            'price_direction': 'down' if price_difference_value > 0 else 'up' if price_difference_value < 0 else 'stable',
            'target_reached': target_reached,
            'was_out_of_stock': entry.was_out_of_stock,
        })

    return payload


def _sort_watchlist_items(items: List[Dict[str, Any]], sort_option: str) -> str:
    normalized_sort = sort_option if sort_option in dict(SORT_CHOICES) else 'newest'
    reverse = True
    key_func = lambda item: item['created_at'] or timezone.now()

    if normalized_sort == 'lowest_price':
        key_func = lambda item: item['current_price_value'] or Decimal('0')
        reverse = False
    elif normalized_sort == 'biggest_drop':
        key_func = lambda item: item['price_drop_value']
    elif normalized_sort == 'highest_discount':
        key_func = lambda item: item['discount_percent']

    items.sort(key=key_func, reverse=reverse)
    return normalized_sort


@login_required
def dashboard_watchlist(request):
    sort_option = request.GET.get('sort', 'newest')
    availability_filter = request.GET.get('availability', 'any')

    items = _build_watchlist_payload(request.user)
    if availability_filter in ('in_stock', 'out_of_stock'):
        items = [item for item in items if item['availability_key'] == availability_filter]

    sort_option = _sort_watchlist_items(items, sort_option)

    context = {
        'items': items,
        'sort_options': SORT_CHOICES,
        'sort_option': sort_option,
        'availability_filters': AVAILABILITY_CHOICES,
        'availability_filter': availability_filter,
    }
    return render(request, 'dashboard/watchlist.html', context)


@login_required
@require_POST
def watchlist_remove(request, uuid):
    watchlist_item = get_object_or_404(Watchlist, uuid=uuid, user=request.user)
    product_name = watchlist_item.product.name
    watchlist_item.delete()
    messages.success(request, f"{product_name} removed from your watchlist.")
    return redirect('dashboard:watchlist')


@login_required
@require_POST
def watchlist_update_target(request, uuid):
    watchlist_item = get_object_or_404(Watchlist, uuid=uuid, user=request.user)
    price_value = request.POST.get('target_price')

    update_fields = ['target_price', 'last_notified_price']
    if price_value:
        try:
            target_price = Decimal(price_value)
            if target_price <= 0:
                raise InvalidOperation()
            watchlist_item.target_price = target_price
            messages.success(request, "Target price updated.")
        except (InvalidOperation, ValueError):
            messages.error(request, "Enter a valid target price.")
            return redirect('dashboard:watchlist')
    else:
        watchlist_item.target_price = None
        messages.success(request, "Target price cleared.")

    watchlist_item.last_notified_price = None
    watchlist_item.save(update_fields=update_fields)
    return redirect('dashboard:watchlist')


@login_required
def dashboard_alerts(request):
    logs = (
        NotificationLog.objects
        .filter(user=request.user)
        .order_by('-intent_timestamp')[:30]
    )
    return render(request, 'dashboard/alerts.html', {'logs': logs})


@login_required
def api_products(request):
    """
    HTMX endpoint returning table rows for products.
    """

    products = (
        Product.objects.filter(is_active=True)
        .prefetch_related(
            Prefetch('prices', queryset=StorePrice.objects.order_by('store_name'))
        )
        .order_by('-updated_at')[:10]
    )

    product_list: List[Dict[str, Any]] = []
    for product in products:
        prices = {price.store_name: price for price in product.prices.all()}
        amz_price = prices.get('Amazon')
        flip_price = prices.get('Flipkart')

        amz_value = amz_price.current_price if amz_price else None
        flip_value = flip_price.current_price if flip_price else None

        numeric_prices = [value for value in [amz_value, flip_value] if value is not None]
        min_value = min(numeric_prices) if numeric_prices else None

        delta_label = 'STABLE_00'
        if amz_value and flip_value:
            delta_amount = Decimal(amz_value) - Decimal(flip_value)
            if delta_amount:
                prefix = 'DROP' if delta_amount > 0 else 'RISE'
                magnitude = abs(delta_amount)
                magnitude_label = (
                    f"{magnitude / Decimal('1000'):.1f}K"
                    if magnitude >= 1000
                    else f"{magnitude:.0f}"
                )
                delta_label = f"{prefix}_{magnitude_label}"

        status = product.trend_indicator or 'LIVE'

        product_list.append(
            {
                'id': product.id,
                'name': product.name.upper()[:24],
                'amz': _format_rupees(amz_value),
                'flip': _format_rupees(flip_value),
                'min': _format_rupees(min_value),
                'delta': delta_label,
                'status': status,
            }
        )

    if not product_list:
        product_list = [
            {'id': 1, 'name': 'IPHONE-14-128-BLK', 'amz': '₹62,999', 'flip': '₹61,499', 'min': '₹61,499', 'delta': 'DROP_1.5K', 'status': 'LIVE'},
            {'id': 2, 'name': 'MACBOOK-AIR-M2-256', 'amz': '₹89,900', 'flip': '₹88,990', 'min': '₹88,990', 'delta': 'STABLE_00', 'status': 'TRACK'},
        ]

    return render(request, 'dashboard/partials/product_rows.html', {'products': product_list})

@login_required
def api_product_history(request, uuid):
    """
    Product-centric price intelligence endpoint.

    - Default: returns JSON payload for existing ApexCharts integration.
    - HTMX `view=panel`: returns a rich product detail + comparison panel
      rendered via `dashboard/partials/product_detail_panel.html`.
    """

    # Always compute the chart payload (used by both JSON and panel views)
    chart_payload = _chart_payload(product_id=uuid)

    # If this is an HTMX request asking for the full detail panel, render HTML
    view_mode = request.GET.get('view') or request.GET.get('mode')
    is_htmx = request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true'

    if is_htmx and view_mode == 'panel':
        from django.shortcuts import get_object_or_404

        product = get_object_or_404(
            Product.objects.select_related('category').prefetch_related(
                Prefetch('prices', queryset=StorePrice.objects.order_by('current_price'))
            ),
            id=uuid,
        )

        # Build store-level comparison data
        stores: List[Dict[str, Any]] = []
        for sp in product.prices.all():
            stores.append(
                {
                    'name': sp.store_name,
                    'price': sp.current_price,
                    'price_display': _format_rupees(sp.current_price),
                    'available': bool(getattr(sp, 'is_available', True)),
                    'availability_label': 'In Stock' if getattr(sp, 'is_available', True) else 'Out of Stock',
                    'last_updated': sp.last_updated,
                    'product_url': sp.product_url,
                }
            )

        # Sort by lowest price first, pushing unknown prices to the end
        def _price_key(row: Dict[str, Any]):
            price = row.get('price')
            if price is None:
                return (1, Decimal('0'))
            try:
                return (0, Decimal(price))
            except Exception:
                return (0, Decimal('0'))

        stores_sorted = sorted(stores, key=_price_key)

        # Derive header metrics
        current_lowest_price = product.current_lowest_price
        if current_lowest_price is None and stores_sorted:
            first_price = stores_sorted[0].get('price')
            if first_price is not None:
                current_lowest_price = first_price

        num_stores = len([s for s in stores_sorted if s.get('price') is not None])

        # Compute insights from full PriceHistory for this product
        history_qs = PriceHistory.objects.filter(store_price__product_id=product.id)
        lowest_price = highest_price = avg_price = None
        change_pct = None
        trend_label = product.trend_indicator or 'STABLE'

        if history_qs.exists():
            prices = list(history_qs.values_list('price', flat=True))
            try:
                lowest_price = min(prices)
                highest_price = max(prices)
                avg_price = sum(prices) / len(prices)
            except Exception:
                lowest_price = highest_price = avg_price = None

            ordered = history_qs.order_by('recorded_at')
            first_point = ordered.first()
            last_point = ordered.last()
            if first_point and last_point and first_point.price:
                try:
                    delta = Decimal(last_point.price) - Decimal(first_point.price)
                    change_pct = (delta / Decimal(first_point.price)) * Decimal('100')
                    if change_pct > 0:
                        trend_label = 'UP'
                    elif change_pct < 0:
                        trend_label = 'DOWN'
                    else:
                        trend_label = 'STABLE'
                except Exception:
                    change_pct = None

        # Watchlist and alert-related context
        is_watchlisted = False
        active_alerts_count = 0
        if request.user.is_authenticated:
            is_watchlisted = Watchlist.objects.filter(user=request.user, product=product).exists()
            # Approximate alerts by matching any store URL for this product
            product_urls = [s['product_url'] for s in stores_sorted if s.get('product_url')]
            if product_urls:
                active_alerts_count = PriceAlert.objects.filter(
                    is_triggered=False,
                    product_url__in=product_urls,
                ).count()

        insights = {
            'lowest_price_display': _format_rupees(lowest_price),
            'highest_price_display': _format_rupees(highest_price),
            'avg_price_display': _format_rupees(avg_price),
            'change_pct_display': f"{change_pct:.1f}%" if change_pct is not None else "N/A",
            'trend_label': trend_label,
            'active_alerts_count': active_alerts_count,
        }

        # Serialize chart payload for the embedded chart widget
        chart_json = json.dumps(chart_payload)

        context = {
            'product': product,
            'stores': stores_sorted,
            'current_lowest_price_display': _format_rupees(current_lowest_price),
            'num_stores': num_stores,
            'trend_label': trend_label,
            'chart_json': chart_json,
            'insights': insights,
            'is_watchlisted': is_watchlisted,
        }

        return render(request, 'dashboard/partials/product_detail_panel.html', context)

    # Default behaviour: JSON payload for existing chart consumers
    return JsonResponse(chart_payload)

@login_required
def api_watchlist(request):
    """
    HTMX endpoint for watchlist panel.
    """

    items = _watchlist_items(request.user if request else None)
    return render(request, 'dashboard/partials/watchlist_items.html', {'items': items})

@login_required
def api_system_health(request):
    """
    HTMX endpoint for system health.
    """

    context = _system_health_snapshot()
    return render(request, 'dashboard/partials/system_health_content.html', context)

@login_required
def api_search(request):
    """
    HTMX POST endpoint for universal search (text).
    """
    query = request.POST.get('q', '').strip()
    if not query:
        return render(request, 'dashboard/partials/search_results.html', {'query': '', 'results': []})

    cleaned = normalize_query(query)
    clean_q = cleaned.get('clean') or query
    service = ScraperService()
    is_htmx = request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true'
    try:
        results = service.scrape(clean_q)
        if not results:
            error_msg = service.last_error or 'Price data unavailable via SerpAPI.'
            if is_htmx:
                return HttpResponse(
                    f'<tr><td colspan="6" class="px-4 py-4 text-center text-[#ff99b5]">{error_msg}</td></tr>',
                )
            return render(
                request,
                'dashboard/partials/search_results.html',
                {
                    'query': clean_q,
                    'results': [],
                    'error': error_msg,
                },
            )

        display_results = [
            {
                'name': r.get('name'),
                'store': r.get('store'),
                'price': _format_rupees(r.get('price')),
                'url': r.get('url'),
                'rating': r.get('rating'),
            } for r in results
        ]

        persisted = service.persist_results(clean_q, cleaned.get('raw'), results)
        product = persisted.get('product')
        rows = persisted.get('rows', [])
        chart = _chart_payload(product_id=product.id) if product else {}

        # If HTMX target is table body, render product rows
        if is_htmx:
            html = render(request, 'dashboard/partials/product_rows.html', {'products': rows}).content.decode('utf-8')
            if chart.get('series'):
                html += """
<script>
if(window.ApexCharts){
    try{
        ApexCharts.exec('priceHistoryChart', 'updateSeries', %s);
        ApexCharts.exec('priceHistoryChart', 'updateOptions', { xaxis: { categories: %s } });
    }catch(e){ console.error('chart update', e); }
}
</script>
""" % (json.dumps(chart.get('series', [])), json.dumps(chart.get('categories', [])))
            return HttpResponse(html)

        # default render search panel results
        return render(
            request,
            'dashboard/partials/search_results.html',
            {
                'query': clean_q,
                'results': display_results,
            },
        )
    except Exception as exc:
        logger.exception('api_search failure')
        return render(request, 'dashboard/partials/search_results.html', {'query': clean_q, 'results': [], 'error': str(exc)})


# Simple in-memory task store for development/demo purposes.
# Key: task_id -> {'status': 'PENDING'|'SUCCESS'|'FAILURE', 'results': [...], 'chart': {...}, 'error': None}
_IMAGE_TASKS: Dict[str, Dict[str, Any]] = {}


def _simulate_image_workflow(task_id: str, image_path: str) -> None:
    """Fallback path when Celery isn't available: run synchronous search/scrape."""
    try:
        time.sleep(1.5)
        query = os.path.basename(image_path)
        cleaned = normalize_query(query)
        service = ScraperService()
        clean_q = cleaned.get('clean') or query
        results = service.scrape(clean_q)
        if not results:
            msg = service.last_error or 'Price data unavailable via SerpAPI.'
            _IMAGE_TASKS[task_id] = {'status': 'FAILURE', 'results': [], 'chart': {}, 'error': msg}
            return

        persisted = service.persist_results(clean_q, cleaned.get('raw'), results)
        product = persisted.get('product')
        chart = _chart_payload(product_id=product.id) if product else {}
        _IMAGE_TASKS[task_id] = {'status': 'SUCCESS', 'results': persisted.get('rows', []), 'chart': chart, 'error': None}
    except Exception as exc:  # pragma: no cover - best-effort demo
        _IMAGE_TASKS[task_id] = {'status': 'FAILURE', 'results': [], 'chart': {}, 'error': str(exc)}


@login_required
def api_image_search(request):
    """Accepts an uploaded image and returns a task id for polling.

    Frontend expects JSON: { task_id: '<id>', ocr_text?: '...' }
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    upload = request.FILES.get('image')
    if not upload:
        return JsonResponse({'error': 'no file'}, status=400)

    # Basic validation: type and size
    allowed_types = {'image/png', 'image/jpeg', 'image/webp'}
    if upload.content_type not in allowed_types:
        return JsonResponse({'error': 'unsupported file type'}, status=400)
    if upload.size and upload.size > 5 * 1024 * 1024:
        return JsonResponse({'error': 'file too large'}, status=400)

    # Save to a temp path under MEDIA_ROOT or fallback to /tmp
    media_root = getattr(settings, 'MEDIA_ROOT', None) or os.path.join(os.getcwd(), 'tmp')
    os.makedirs(media_root, exist_ok=True)
    filename = f"img_{uuid.uuid4().hex[:8]}.png"
    path = os.path.join(media_root, filename)
    with open(path, 'wb') as fh:
        for chunk in upload.chunks():
            fh.write(chunk)

    task_id = uuid.uuid4().hex
    _IMAGE_TASKS[task_id] = {'status': 'PENDING', 'results': [], 'chart': {}, 'error': None}

    # Run OCR synchronously to extract text, then enqueue a Celery task with cleaned query
    raw_text = ''
    # import OCR libraries lazily to avoid crashing if not installed
    try:
            import pytesseract
            from PIL import Image
            from django.conf import settings as dj_settings
            img = Image.open(path)
            # If a TESSERACT_CMD is configured, explicitly point pytesseract at it
            tesseract_path = getattr(dj_settings, 'TESSERACT_CMD', None)
            if tesseract_path:
                pytesseract.pytesseract.tesseract_cmd = tesseract_path
                logger.info("Using Tesseract binary: %s", pytesseract.pytesseract.tesseract_cmd)
            raw_text = pytesseract.image_to_string(img)
    except ModuleNotFoundError as me:
        logger.warning('OCR libs missing: %s', me)
        raw_text = ''
    except Exception:
        logger.exception('OCR error')
        raw_text = ''

    cleaned = normalize_query(raw_text or os.path.basename(path))
    logger.debug('OCR RAW: %s', raw_text)
    logger.debug('OCR CLEAN: %s', cleaned.get('clean'))

    # Try to queue a Celery task; fallback to background thread if Celery not available
    try:
        async_result = image_search_task.delay(path, cleaned.get('clean'), task_id)
        # store placeholder that will be updated by polling via result() when ready
        _IMAGE_TASKS[task_id] = {'status': 'PENDING', 'celery_id': async_result.id, 'results': [], 'chart': {}, 'error': None}
    except Exception:
        # fallback to thread that runs the same flow
        t = threading.Thread(target=_simulate_image_workflow, args=(task_id, path), daemon=True)
        t.start()

    is_htmx = request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true'
    if is_htmx:
        return render(request, 'dashboard/partials/image_loading.html', {'task_id': task_id}, status=202)
    # Return OCR text (if any) so frontend can populate the search box
    return JsonResponse({'task_id': task_id, 'ocr_text': cleaned.get('clean')}, status=202)


@login_required
def api_result(request, task_id: str):
    """Poll endpoint for image workflow results.

    Returns JSON with shape: { status: 'PENDING'|'SUCCESS'|'FAILURE', results: [...], chart: {...}, error: None }
    """
    # Prefer cached task payload from Redis if available (written by Celery worker)
    try:
        import redis
        import json as _json
        r = redis.Redis(host='127.0.0.1', port=6379, db=0, socket_timeout=5)
        # Temporary test injection when calling TEST123
        try:
            if task_id == 'TEST123' and not r.exists(f"pricecom:task:{task_id}"):
                sample = {"status": "SUCCESS", "results": [{"name": "TEST PRODUCT", "amz": "₹1,000", "flip": "₹950", "min": "₹950", "delta": "DROP_50", "status": "LIVE"}], "chart": {}}
                r.setex(f"pricecom:task:{task_id}", 3600, _json.dumps(sample))
        except Exception:
            pass

        raw = r.get(f"pricecom:task:{task_id}")
        logger.debug('API RESULT RAW REDIS: %s', raw)
        if raw:
            try:
                cached = _json.loads(raw)
                try:
                    logger.debug('API RESULT CACHED: %s', _json.dumps(cached, ensure_ascii=True))
                except Exception:
                    logger.debug('API RESULT CACHED: <unprintable>')
                # If Redis already has results, render/return immediately
                if isinstance(cached, dict) and cached.get('results'):
                    results = cached.get('results') or []
                    html = render(request, 'dashboard/partials/product_rows.html', {'products': results}).content.decode('utf-8')
                    if request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true':
                        return HttpResponse(html)
                    return JsonResponse(cached)
                # Otherwise populate in-memory fallback for later logic
                redis_task = {'status': cached.get('status', 'PENDING'), 'results': cached.get('results', []), 'chart': cached.get('chart', {}), 'error': cached.get('error')}
                _IMAGE_TASKS[task_id] = redis_task
            except Exception:
                redis_task = None
                pass
    except Exception as e:
        print('API RESULT REDIS ERROR:', e)
        # If Redis not available, fall back to in-memory store
        pass

    # Prefer Redis-populated task when available
    task = locals().get('redis_task') or _IMAGE_TASKS.get(task_id)
    print("API RESULT TASK:", task_id)
    try:
        import json as _print_json
        print("REDIS DATA:", _print_json.dumps(task, ensure_ascii=True) if task is not None else None)
    except Exception:
        print("REDIS DATA: <unprintable>")
    if task is None:
        if request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true':
            return HttpResponse('<tr><td colspan="6" class="px-4 py-4 text-center text-brand-textMuted">Task not found</td></tr>', status=404)
        return JsonResponse({'error': 'not found'}, status=404)

    # If task was enqueued to Celery, check result and populate
    celery_id = task.get('celery_id') if isinstance(task, dict) else None
    if celery_id:
        try:
            from celery.result import AsyncResult
            res = AsyncResult(celery_id)
            if res.ready():
                data = res.result or {}
                # normalize into our task dict
                task.update({'status': data.get('status', 'SUCCESS' if data else 'SUCCESS'), 'results': data.get('results', []), 'chart': data.get('chart', {}), 'error': data.get('error')})
                _IMAGE_TASKS[task_id] = task
                task = _IMAGE_TASKS[task_id]
        except Exception:
            # keep existing PENDING state
            pass

    is_htmx = request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true'
    if not is_htmx:
        # allow JSON format explicitly
        if request.GET.get('format') == 'json':
            return JsonResponse(_IMAGE_TASKS[task_id])
        return JsonResponse(_IMAGE_TASKS[task_id])

    # HTMX response: return table rows for success, otherwise status row
    status_val = task.get('status') if isinstance(task, dict) else None
    chart_payload = task.get('chart') if isinstance(task, dict) else None

    # If task contains results, render them regardless of explicit SUCCESS state
    if isinstance(task, dict) and task.get('results'):
        results = task.get('results') or []
        if not results:
            return HttpResponse('<tr><td colspan="6" class="px-4 py-4 text-center text-brand-textMuted">No results found</td></tr>')

        html = render(request, 'dashboard/partials/product_rows.html', {'products': results}).content.decode('utf-8')

        if chart_payload and isinstance(chart_payload, dict) and chart_payload.get('series'):
            html += """
<script>
if(window.ApexCharts){
    try{
        const series = %s;
        const categories = %s;
        ApexCharts.exec('priceHistoryChart', 'updateSeries', series);
        if(categories){ ApexCharts.exec('priceHistoryChart', 'updateOptions', { xaxis: { categories: categories } }); }
    }catch(e){ console.error('chart update error', e); }
}
</script>
""" % (json.dumps(chart_payload.get('series')), json.dumps(chart_payload.get('categories')))

        if request.headers.get('HX-Request') == 'true' or request.META.get('HTTP_HX_REQUEST') == 'true':
            return HttpResponse(html)
        # Non-HTMX callers get JSON
        return JsonResponse(task)

    if status_val == 'FAILURE':
        msg = task.get('error') or 'Processing failed'
        return HttpResponse(f'<tr><td colspan="6" class="px-4 py-4 text-center text-[#ff99b5]">{msg}</td></tr>', status=500)

    # Pending state fallback
    return HttpResponse('<tr><td colspan="6" class="px-4 py-8 text-center text-brand-textMuted"><span class="h-4 w-4 border-2 border-brand-accent border-t-transparent inline-block animate-spin mr-2"></span>Processing image…</td></tr>', status=202)
