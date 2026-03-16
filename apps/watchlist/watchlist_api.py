from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
from .watchlist_service import WatchlistService

@csrf_exempt
@require_http_methods(["POST"])
def add_to_watchlist(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)
        
    try:
        data = json.loads(request.body)
        result = WatchlistService.add_to_watchlist(request.user.id, data)
        if result.get("success"):
            return JsonResponse(result, status=201)
        return JsonResponse(result, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

@require_http_methods(["GET"])
def get_watchlist(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)
        
    items = WatchlistService.get_watchlist(request.user.id)
    return JsonResponse({"items": items})

@csrf_exempt
@require_http_methods(["DELETE", "POST"])
def remove_from_watchlist(request, product_id):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)
        
    success = WatchlistService.remove_from_watchlist(request.user.id, product_id)
    if success:
        return JsonResponse({"success": True, "message": "Removed from watchlist"})
    return JsonResponse({"success": False, "error": "Could not remove product"}, status=400)
