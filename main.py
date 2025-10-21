import asyncio
import base64
import hashlib
import hmac
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort
# FIX: Explicitly import timedelta to avoid name collision with the datetime class
import requests
from datetime import datetime, timedelta
import pymssql, shopify
import aiohttp
import lazop
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout
from aiohttp import ClientSession, ClientError
import pytz

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')
pre_loaded = 0
order_details = []
semaphore = asyncio.Semaphore(2) # Limit to 2 concurrent API calls
product_cache = {} # Global cache for product data to reduce Shopify API calls


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
async def fetch_with_retry(session, url, method="GET", **kwargs):
    """Fetch data with retry logic."""
    async with session.request(method, url, **kwargs) as response:
        if response.status == 429:  # Rate limit exceeded
            retry_after = int(response.headers.get("Retry-After", 1))
            print(f"Rate limit hit. Retrying after {retry_after} seconds...")
            await asyncio.sleep(retry_after)
        response.raise_for_status()
        return await response.json()


async def limited_request(coroutine):
    """Ensure requests adhere to rate limits by using a semaphore and delay."""
    async with semaphore:
        await asyncio.sleep(0.5)  # Enforce delay for Shopify rate limits (no more than 2 per second)
        return await coroutine


@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    to_emails = data.get('to', [])
    cc_emails = data.get('cc', [])
    subject = data.get('subject', '')
    body = data.get('body', '')

    try:
        # SMTP server configuration
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')
        smtp_password = os.getenv('SMTP_PASSWORD')

        # Create the message
        msg = MIMEText(body)
        msg['From'] = smtp_user
        msg['To'] = ', '.join(to_emails)
        msg['Cc'] = ', '.join(cc_emails)
        msg['Subject'] = subject

        # Connect to the SMTP server and send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()

        return jsonify({'message': 'Email sent successfully'}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
async def fetch_tracking_data(session, tracking_number):
    url = f"https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory?cn={tracking_number}"

    # Set a timeout of 10 seconds for the request
    timeout = ClientTimeout(total=10)

    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status == 200:
                return await response.json()
            else:
                print(f"Error: HTTP {response.status} for tracking number {tracking_number}")
                return {"error": f"HTTP {response.status}"}
    except ClientError as e:
        print(f"Request failed: {e}")
        return {"error": str(e)}
    except asyncio.TimeoutError:
        print(f"Request timed out for tracking number {tracking_number}")
        return {"error": "Request timed out"}


def fetch_product_details_sync(product_id, variant_id):
    """
    Synchronous function to fetch product and variant details from Shopify.
    This function runs in a separate thread pool to prevent blocking the async loop.
    It uses caching to drastically reduce API calls.
    """
    default_image = "https://static.thenounproject.com/png/1578832-200.png"
    try:
        # 1. Use Cache
        if product_id in product_cache:
            product = product_cache[product_id]
        else:
            # 2. Fetch Product (Blocking call)
            product = shopify.Product.find(product_id)
            if product:
                product_cache[product_id] = product
            else:
                return default_image, ""

        image_src = default_image
        variant_name = ""

        # 3. Find Variant and Image
        if product.variants:
            for variant in product.variants:
                if variant.id == variant_id:
                    variant_name = variant.title or ""

                    # Prioritize variant image if available on the product object
                    variant_image_src = next((img.src for img in product.images if img.id == variant.image_id), None)

                    if variant_image_src:
                        image_src = variant_image_src
                    elif product.image:
                         # Fallback to the main product image
                         image_src = product.image.src

                    break

        return image_src, variant_name

    except Exception as e:
        # Catch specific exceptions from Shopify API if possible
        print(f"Error in fetch_product_details_sync for product ID {product_id}: {e}")
        return default_image, ""


async def process_line_item(session, line_item, fulfillments):
    """Process individual line items and tracking status."""
    if line_item.fulfillment_status is None and line_item.fulfillable_quantity == 0:
        return []

    tracking_info = []

    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if fulfillment.status == "cancelled":
                continue

            for item in fulfillment.line_items:
                if item.id == line_item.id:
                    tracking_number = fulfillment.tracking_number
                    data = await fetch_tracking_data(session, tracking_number)
                    if isinstance(data, dict) and data.get('error'):
                         tracking_details = "N/A" # Default if tracking fetch failed
                    else:
                         tracking_details = data[-1].get('ProcessDescForPortal', 'DELIVERED') if data and isinstance(data, list) else "N/A"


                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': tracking_details,
                        'quantity': item.quantity
                    })

    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": line_item.quantity}
    ]


async def process_order(session, order):
    """Process each order, ensuring synchronous Shopify API calls are threaded and rate-limited."""
    try:
        order_start_time = time.time()
        
        # 1. Prepare Order Meta Data
        created_at_str = order.created_at
        created_at_obj = datetime.fromisoformat(created_at_str)
        formatted_date = created_at_obj.strftime('%Y-%m-%d')
        
        order_info = {
            'order_link': "https://admin.shopify.com/store/alkaramat/orders/" + str(order.id),
            'order_num': order.name.replace("#", ""),
            'order_id': order.id,
            'created_at': formatted_date,
            'total_price': order.current_subtotal_price,
            'line_items': [],
            'financial_status': order.financial_status.title(),
            'fulfillment_status': order.fulfillment_status or "Unfulfilled",
            'customer_details': {
                "name": getattr(order.shipping_address, "name", " "),
                "address": getattr(order.shipping_address, "address1", " "),
                "city": getattr(order.shipping_address, "city", " "),
                "phone": getattr(order.shipping_address, "phone", " ")
            },
            'tags': order.tags.split(", ") if order.tags else []
        }

        # 2. Concurrently fetch tracking data (AIOHTTP calls - already async)
        tracking_tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        tracking_results = await asyncio.gather(*tracking_tasks)
        
        # 3. Concurrently fetch product details (Synchronous Shopify API calls - must be threaded and rate-limited)
        loop = asyncio.get_event_loop()
        product_detail_tasks = []
        
        for line_item in order.line_items:
            if line_item.product_id is not None:
                # Wrap the synchronous fetch in limited_request and run_in_executor
                task = limited_request(
                    loop.run_in_executor(
                        None, 
                        fetch_product_details_sync, 
                        line_item.product_id, 
                        line_item.variant_id
                    )
                )
            else:
                # Immediate execution for items without product_id
                task = loop.run_in_executor(None, lambda: ("https://static.thenounproject.com/png/1578832-200.png", ""))

            product_detail_tasks.append(task)
            
        product_details = await asyncio.gather(*product_detail_tasks, return_exceptions=True)

        # 4. Combine Results
        for (tracking_info_list, line_item), product_info in zip(zip(tracking_results, order.line_items), product_details):
            if tracking_info_list is None:
                continue
            
            # Handle potential error from the product detail fetch
            if isinstance(product_info, Exception):
                print(f"Error fetching product details for product ID {line_item.product_id}: {product_info}")
                image_src = "https://static.thenounproject.com/png/1578832-200.png"
                variant_name = line_item.variant_title or ""
            else:
                image_src, raw_variant_name = product_info
                
                # Use variant name from sync fetch if available, otherwise fall back
                variant_name = raw_variant_name if raw_variant_name else (line_item.variant_title or "")
            
            # Append tracking and line item details
            for info in tracking_info_list:
                order_info['line_items'].append({
                    'fulfillment_status': line_item.fulfillment_status,
                    'image_src': image_src,
                    # Construct product title
                    'product_title': line_item.title + (f" - {variant_name}" if variant_name else ""),
                    'quantity': info['quantity'],
                    'tracking_number': info['tracking_number'],
                    'status': info['status']
                })
                order_info['status'] = info['status'] # Overwrites status with the last item's status, which might be incorrect but reflects the original logic

        order_end_time = time.time()
        print(f"Processed order {order.order_number} in {order_end_time - order_start_time:.2f} seconds")
        return order_info

    except Exception as e:
        print(f"Error processing order {order.order_number}: {e}")
        return None

@app.route('/pending')
def pending_orders():
    all_orders = []
    pending_items_dict = {}  # Dictionary to track quantities of each unique item

    global order_details

    # Process Shopify orders with the specified statuses
    for shopify_order in order_details:
        if not shopify_order:  # Skip if shopify_order is None or empty
            continue
        if shopify_order.get('status') in ['CONSIGNMENT BOOKED', 'Un-Booked']:
            shopify_items_list = [
                {
                    'item_image': item['image_src'],
                    'item_title': item['product_title'],
                    'quantity': item['quantity'],
                    'tracking_number': item['tracking_number'],
                    'status': item['status']
                }
                for item in shopify_order['line_items']
            ]

            shopify_order_data = {
                'order_link' : shopify_order['order_link'],
                'order_via': 'Shopify',
                'order_id': shopify_order['order_id'],
                'order_num': shopify_order['order_num'],
                'status': shopify_order.get('status'),
                'tracking_number': shopify_order.get('tracking_number', 'N/A'),
                'date': shopify_order['created_at'],
                'items_list': shopify_items_list,
                'total_price': shopify_order['total_price']
            }
            all_orders.append(shopify_order_data)

            # Count quantities for each item in the Shopify order
            for item in shopify_items_list:
                product_title = item['item_title']
                quantity = item['quantity']
                item_image = item['item_image']

                if product_title in pending_items_dict:
                    pending_items_dict[product_title]['quantity'] += quantity
                else:
                    pending_items_dict[product_title] = {
                        'item_image': item_image,
                        'item_title': product_title,
                        'quantity': quantity
                    }

    pending_items = list(pending_items_dict.values())
    pending_items_sorted = sorted(
        pending_items,
        key=lambda x: x.get('item_title', '').lower() if x.get('item_title') else '',
        reverse=True
    )

    half = len(pending_items_sorted) // 2

    return render_template('pending.html', all_orders=all_orders, pending_items=pending_items_sorted, half=half)



async def getShopifyOrders():
    # Only pull orders from the last 10 months to manage list size
    # FIX: Use timedelta directly now that it's imported
    start_date = (datetime.now() - timedelta(days=300)).isoformat()
    order_details = []
    total_start_time = time.time()

    try:
        # Initial call to get the first page
        orders = shopify.Order.find(limit=50, order="created_at DESC", created_at_min=start_date)
    except Exception as e:
        print(f"Error fetching initial orders: {e}")
        return []

    async with aiohttp.ClientSession() as session:
        while True:
            # Prepare tasks for the current page
            # Note: We do not wrap process_order with limited_request here, 
            # as the rate limiting is handled internally for the sync API calls within process_order.
            tasks = [process_order(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing an order: {result}")
                elif result is not None:
                    order_details.append(result)

            try:
                # Check for next page
                if not orders.has_next_page():
                    break
                orders = orders.next_page()

            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    total_end_time = time.time()
    print(f"Processed {len(order_details)} orders in {total_end_time - total_start_time:.2f} seconds")
    return order_details


def adjust_to_shopify_timezone(from_date, to_date):
    # Parse input dates (assumed to be in YYYY-MM-DD)
    from_date_obj = datetime.strptime(from_date, "%Y-%m-%d")
    to_date_obj = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    # Assume GMT+5 is the local shopify timezone for explicit formatting
    # Note: Shopify API expects ISO 8601 with offset
    gmt_plus_5 = pytz.timezone('Asia/Karachi') # Using a known GMT+5 timezone
    
    from_date_gmt_plus_5 = gmt_plus_5.localize(from_date_obj).isoformat()
    to_date_gmt_plus_5 = gmt_plus_5.localize(to_date_obj).isoformat()


    return from_date_gmt_plus_5, to_date_gmt_plus_5


async def getShopifyOrderswithDates(start_date: str, end_date: str):
    order_details = []
    total_start_time = time.time()

    try:
        orders = shopify.Order.find(
            limit=50,
            order="created_at DESC",
            created_at_min=start_date,
            created_at_max=end_date,
            status='any'
        )
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return []

    async with aiohttp.ClientSession() as session:
        while True:
            tasks = [process_order(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing an order: {result}")
                elif result is not None:
                    order_details.append(result)

            try:
                if not orders.has_next_page():
                    break
                orders = orders.next_page()
            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    total_end_time = time.time()
    print(f"Processed {len(order_details)} orders in {total_end_time - total_start_time:.2f} seconds")
    return order_details


@app.route('/fetch-orders', methods=['POST'])
def fetch_orders():
    data = request.get_json()

    from_date = data.get('fromDate')
    to_date = data.get('toDate')

    from_date_iso, to_date_iso = adjust_to_shopify_timezone(from_date, to_date)
    print(f"Fetching from {from_date_iso} to {to_date_iso}")

    orders = asyncio.run(getShopifyOrderswithDates(from_date_iso, to_date_iso))

    total_sales = 0.0
    for order in orders:
        try:
            total_sales += float(order['total_price'])
        except (TypeError, ValueError):
            print(f"Warning: Could not parse total_price for order {order.get('order_num', 'N/A')}")
            pass

    return jsonify({
        'orders': orders,
        'total_sales': total_sales,
        'total_cost': 0.0
    })


@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    print(order_id)
    tag = data.get('tag')

    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"
    try:
        order = shopify.Order.find(order_id)

        if not hasattr(order, 'tags'):
            raise ValueError("Could not fetch a valid order. Ensure the order ID is correct.")

        if tag.strip().lower() == "returned":
            if order.cancel():
                print("Order Cancelled")
            else:
                print("Order Cancellation Failed")
        if tag.strip().lower() == "delivered":
            if order.close():
                print("Order Closed")
            else:
                print("Order Closing Failed")

        tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []

        if "Leopards Courier" in tags:
            tags.remove("Leopards Courier")

        if tag_with_date not in tags:
            tags.append(tag_with_date)

        order.tags = ", ".join(tags)
        if order.save():
            return jsonify({"success": True, "message": "Tag applied successfully."})
        else:
            return jsonify({"success": False, "error": "Failed to save order changes."})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/")
def tracking():
    global order_details
    return render_template("track_alk.html", order_details=order_details)


def format_date(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
    return date_obj.strftime("%Y-%m-%d")


@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details, product_cache
    # Clear cache on refresh
    product_cache = {}
    try:
        order_details = asyncio.run(getShopifyOrders())
        return render_template("track_alk.html", order_details=order_details)
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({'message': 'Failed to refresh data'}), 500


def run_async(func, *args, **kwargs):
    return asyncio.run(func(*args, **kwargs))


@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    print(f"Tracking Number: {tracking_num}")

    async def async_func():
        async with aiohttp.ClientSession() as session:
            return await fetch_tracking_data(session, tracking_num)

    data = run_async(async_func)

    return render_template('trackingdata_alk.html', data=data)


@app.route('/undelivered')
def undelivered():
    global order_details, pre_loaded
    return render_template("undelivered.html", order_details=order_details)


@app.route('/report')
def report():
    global order_details, pre_loaded
    if not isinstance(order_details, list):
        order_details = []
    return render_template("report.html", order_details=order_details)


@app.route("/track")
def tracking_track():
    global order_details
    return render_template("track_alk.html", order_details=order_details)


def verify_shopify_webhook(request):
    """
    Verify the webhook using HMAC with the shared secret.
    """
    shopify_hmac = request.headers.get('X-Shopify-Hmac-Sha256')
    data = request.get_data()

    secret = os.getenv('SHOPIFY_WEBHOOK_SECRET')

    if secret is None:
        print("SECURITY ALERT: SHOPIFY_WEBHOOK_SECRET is not set.")
        return False

    digest = hmac.new(
        secret.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')

    return hmac.compare_digest(computed_hmac, shopify_hmac)


@app.route('/shopify/webhook/order_updated', methods=['POST'])
def shopify_order_updated():
    global order_details
    try:
        if not verify_shopify_webhook(request):
            print("Webhook verification failed (HMAC mismatch or no secret).")
            return jsonify({'error': 'Invalid webhook signature'}), 401

        order_data = request.get_json()
        order_shopify_id = order_data.get('id')
        if not order_shopify_id:
            return jsonify({'error': 'No order id found in payload'}), 400

        print(f"Received webhook for order Shopify ID: {order_shopify_id}")

        if order_data.get('closed_at'):
            print(f"Order {order_shopify_id} is closed. Attempting removal from list.")
            order_details[:] = [o for o in order_details if o.get('order_id') != order_shopify_id]
            return jsonify({'success': True, 'message': f'Order {order_shopify_id} removed.'}), 200

        # Fetch and process the updated order
        order = shopify.Order.find(order_shopify_id)
        if not order:
            return jsonify({'error': f'Order {order_shopify_id} not found'}), 404

        async def update_order():
            async with aiohttp.ClientSession() as session:
                return await process_order(session, order)

        updated_order_info = asyncio.run(update_order())

        order_num_to_match = updated_order_info.get('order_num')

        updated = False
        for idx, existing_order in enumerate(order_details):
            if existing_order.get('order_num') == order_num_to_match:
                order_details[idx] = updated_order_info
                updated = True
                break
        if not updated:
            order_details.append(updated_order_info)

        return jsonify({
            'success': True,
            'message': f'Order {order_num_to_match} processed successfully',
            'order': updated_order_info
        }), 200

    except Exception as e:
        print(f"Webhook processing error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


##SCANNER

@app.route('/scanner')
def scanner_page():
    """Renders the scanner HTML page."""
    return render_template('scanner.html')


@app.route('/api/scan/order', methods=['POST'])
def scan_single_order():
    scanned_value = request.json.get('scan_input')

    if not scanned_value:
        return jsonify({'error': 'No input provided'}), 400

    scan_term = str(scanned_value).strip().replace("#", "")

    found_order = None

    # 1. Search by Shopify Order Number (e.g., '9899300')
    found_order = next((o for o in order_details if o.get('order_num') == scan_term), None)

    # 2. If not found, search by Tracking Number within the line_items of ALL orders
    if not found_order:
        for order in order_details:
            if order.get('line_items'):
                for item in order['line_items']:
                    if item.get('tracking_number') == scan_term:
                        found_order = order
                        break
                if found_order:
                    break

    if found_order:
        items_list = [
            {
                'title': item['product_title'],
                'quantity': item['quantity'],
                'image_src': item['image_src']
            }
            for item in found_order['line_items']
        ]

        return jsonify({
            'success': True,
            'order_num': found_order['order_num'],
            'order_id': found_order['order_id'],
            'customer': found_order['customer_details']['name'],
            'items': items_list
        }), 200
    else:
        return jsonify({'success': False, 'error': f'Order/Tracking ID "{scan_term}" not found in loaded orders.'}), 404


@app.route('/api/apply_bulk_tag', methods=['POST'])
def apply_bulk_tag():
    data = request.json
    order_ids_to_tag = data.get('order_ids', [])
    tag_type = data.get('tag_type')

    if not order_ids_to_tag or tag_type not in ['RETURNED', 'DISPATCHED']:
        return jsonify({"success": False, "error": "Invalid input or tag type."}), 400

    today_date = datetime.now().strftime('%Y-%m-%d')
    results = []

    for order_shopify_id in order_ids_to_tag:
        try:
            if tag_type == 'RETURNED':
                base_tag = "Return Received"
            elif tag_type == 'DISPATCHED':
                base_tag = "DISPATCHED"

            final_tag = f"{base_tag} ({today_date})"

            order = shopify.Order.find(order_shopify_id)
            if not order:
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Order not found.'})
                continue

            tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []

            if final_tag not in tags:
                tags.append(final_tag)

            order.tags = ", ".join(tags)
            if order.save():
                results.append({'id': order_shopify_id, 'status': 'success', 'message': f'Tag "{final_tag}" applied.'})
            else:
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Failed to save order changes on Shopify.'})

        except Exception as e:
            results.append({'id': order_shopify_id, 'status': 'error', 'message': str(e)})

    return jsonify({
        'success': True,
        'tag_applied': final_tag,
        'total_orders': len(order_ids_to_tag),
        'results': results
    }), 200

# Shopify Configuration
shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)

# Initial Data Load
try:
    order_details = asyncio.run(getShopifyOrders())
except Exception as e:
    print(f"Initial order loading failed: {e}")
    order_details = []


if __name__ == "__main__":
    app.run(port=5001)

