from django.views.generic import TemplateView, View, ListView
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.utils.decorators import method_decorator
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required

import logging
from datetime import timedelta

from apps.scraper.services.manager import get_coordinated_data
from apps.scraper.decorators import simple_ratelimit
from apps.scraper.models import Product, Watchlist, PriceHistory
from apps.scraper.security_utils import verify_signature

from django.conf import settings
from django.utils import timezone

# NEW: Celery imports
from celery.result import AsyncResult
from config.celery import app as celery_app


logger = logging.getLogger(__name__)


class ProductSearchView(TemplateView):
    template_name = "scraper/dashboard.html"

    @method_decorator(simple_ratelimit(key_prefix="search", limit=10, period=60))
    def get(self, request, *args, **kwargs):
        search_query = request.GET.get("q", "")

        if search_query:
            user_info = (
                f"User: {request.user.id} ({request.user.email})"
                if request.user.is_authenticated
                else "Anon"
            )
            ip = request.META.get("REMOTE_ADDR")

            logger.info(
                f"AUDIT: Search Query='{search_query}' by {user_info} IP={ip}"
            )

        context = self.get_context_data(**kwargs)
        context["search_query"] = search_query

        return self.render_to_response(context)

    @method_decorator(simple_ratelimit(key_prefix="search", limit=10, period=60))
    def post(self, request, *args, **kwargs):

        search_query = request.POST.get("q", "")

        results = get_coordinated_data(search_query)

        return render(
            request,
            "scraper/partials/dashboard_results.html",
            {"results": results},
        )


# ✅ FIXED: Celery-compatible TaskStatusView
class TaskStatusView(View):

    def get(self, request, task_id, *args, **kwargs):

        result = AsyncResult(task_id, app=celery_app)

        if result.state == "SUCCESS":
            return JsonResponse({
                "status": "completed",
                "result": result.result
            })

        elif result.state == "PENDING":
            return JsonResponse({"status": "processing"})

        elif result.state == "FAILURE":
            return JsonResponse({
                "status": "failed",
                "error": str(result.result)
            })

        return JsonResponse({"status": result.state})


class WatchlistView(LoginRequiredMixin, ListView):

    model = Watchlist
    template_name = "scraper/watchlist.html"
    context_object_name = "watchlist"

    def get_queryset(self):

        return (
            Watchlist.objects.filter(user=self.request.user)
            .select_related("product")
            .prefetch_related("product__prices")
        )


class ToggleWatchlistView(LoginRequiredMixin, View):

    def post(self, request, *args, **kwargs):

        product_id = request.POST.get("product_id")

        if not product_id:
            return JsonResponse({"error": "Invalid payload"}, status=400)

        try:
            product = Product.objects.get(id=product_id)

        except Product.DoesNotExist:
            return JsonResponse({"error": "Product not found"}, status=404)

        watchlist_item, created = Watchlist.objects.get_or_create(
            user=request.user,
            product=product,
        )

        if created:
            initial_price = product.current_lowest_price or product.base_price
            availability = product.prices.filter(is_available=True).exists()
            fields_to_update = []

            if initial_price:
                watchlist_item.added_price = initial_price
                watchlist_item.last_recorded_price = initial_price
                fields_to_update.extend(['added_price', 'last_recorded_price'])

            watchlist_item.was_out_of_stock = not availability
            fields_to_update.append('was_out_of_stock')

            watchlist_item.save(update_fields=list(set(fields_to_update)))

        if not created:

            watchlist_item.delete()

            new_btn = f"""
            <button hx-post="/scraper/watchlist/toggle/"
                    hx-vals='{{"product_id": "{product.id}"}}'
                    hx-target="this"
                    hx-swap="outerHTML"
                    onclick="event.preventDefault(); event.stopPropagation();"
                    class="bg-transparent text-brand-accent border border-brand-accent px-3 py-1 text-[10px] uppercase tracking-widest hover:bg-brand-accent hover:text-[#080B0F] transition-none">
                Add to Watchlist
            </button>
            """

        else:

            new_btn = f"""
            <button hx-post="/scraper/watchlist/toggle/"
                    hx-vals='{{"product_id": "{product.id}"}}'
                    hx-target="this"
                    hx-swap="outerHTML"
                    onclick="event.preventDefault(); event.stopPropagation();"
                    class="bg-brand-accent text-[#080B0F] border border-brand-accent px-3 py-1 text-[10px] uppercase tracking-widest hover:bg-transparent hover:text-brand-accent transition-none">
                Remove from Watchlist
            </button>
            """

        return HttpResponse(new_btn)


class PriceHistoryAPIView(View):

    def get(self, request, product_id, *args, **kwargs):

        try:
            product = Product.objects.get(id=product_id)

        except Product.DoesNotExist:
            return JsonResponse({"error": "Product not found"}, status=404)

        seven_days_ago = timezone.now() - timedelta(days=7)

        history_qs = (
            PriceHistory.objects.filter(
                store_price__product=product,
                recorded_at__gte=seven_days_ago,
            )
            .select_related("store_price")
        )

        data = []

        for h in history_qs:

            if not h.data_signature:
                verification_status = "unsigned"

            else:

                data_dict = {
                    "price": str(h.price),
                    "currency": "INR",
                }

                if verify_signature(
                    settings.SECRET_KEY, data_dict, h.data_signature
                ):
                    verification_status = "verified"

                else:
                    verification_status = "tampered"

            data.append(
                {
                    "price": h.price,
                    "store": h.store_price.store_name,
                    "date": h.recorded_at.strftime("%Y-%m-%d %H:%M"),
                    "status": verification_status,
                }
            )

        return JsonResponse({"product": product.name, "history": data})


@login_required
@require_POST
def set_target_price(request):

    watchlist_id = request.POST.get("watchlist_id")
    target_price = request.POST.get("target_price")

    if not watchlist_id:
        return JsonResponse({"error": "Missing watchlist_id"}, status=400)

    try:
        watch = Watchlist.objects.get(
            id=watchlist_id,
            user=request.user,
        )

    except Watchlist.DoesNotExist:
        return JsonResponse(
            {"error": "Watchlist item not found"},
            status=404,
        )

    try:
        watch.target_price = float(target_price)
        watch.last_notified_price = None

    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid target price"}, status=400)

    watch.save()

    return JsonResponse(
        {
            "success": True,
            "target_price": watch.target_price,
        }
    )