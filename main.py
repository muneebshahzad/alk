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
import datetime, requests
from datetime import datetime
import pymssql, shopify
import aiohttp
import lazop
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout
from aiohttp import ClientSession, ClientError
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
import pytz
from apscheduler.schedulers.background import BackgroundScheduler # NEW IMPORT
import threading # Used for thread-safe global access management (optional, but good practice)

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')
pre_loaded = 0
order_details = []
semaphore = asyncio.Semaphore(2)

# --- APScheduler Setup ---
scheduler = BackgroundScheduler()
# Lock for thread-safe access to global order_details
data_lock = threading.Lock()
# --- END APScheduler Setup ---


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
    """Ensure requests adhere to rate limits."""
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
    timeout = ClientTimeout(total=100)
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

product_cache = {}

async def process_line_item(session, line_item, fulfillments):
    """Process individual line items."""
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
                    if data and isinstance(data, list) and data:
                        tracking_details = data[-1].get('ProcessDescForPortal', 'DELIVERED')
                    else:
                        tracking_details = "N/A"
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': tracking_details,
                        'quantity': item.quantity
                    })
    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": line_item.quantity}
    ]

async def process_order(session, order):
    """Process each order with optimized image fetching."""
    try:
        order_start_time = time.time()
        created_at_str = order.created_at
        created_at_obj = datetime.fromisoformat(created_at_str.replace('Z', '+00:00')) # Ensure Z is handled
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
        
        tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        results = await asyncio.gather(*tasks)

        for tracking_info_list, line_item in zip(results, order.line_items):
            if tracking_info_list is None:
                continue
            
            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = line_item.variant_title or ""
            
            # Optimized Image/Variant Name Fetch (still synchronous/blocking, consider optimization)
            if line_item.product_id is not None:
                try:
                    product = shopify.Product.find(line_item.product_id)
                    if product and product.variants:
                        for variant in product.variants:
                            if variant.id == line_item.variant_id:
                                # Fetch image source from variant or product image
                                if variant.image_id is not None:
                                    images = shopify.Image.find(image_id=variant.image_id, product_id=line_item.product_id)
                                    for image in images:
                                        if image.id == variant.image_id:
                                            image_src = image.src
                                            break
                                elif product.image:
                                    image_src = product.image.src
                                break
                    else:
                        image_src = "https://static.thenounproject.com/png/1578832-200.png"
                except Exception as e:
                    print(f"Error fetching product details for product ID {line_item.product_id}: {e}")
            
            # Append tracking and line item details
            for info in tracking_info_list:
                order_info['line_items'].append({
                    'fulfillment_status': line_item.fulfillment_status,
                    'image_src': image_src,
                    'product_title': line_item.title + (f" - {variant_name}" if variant_name else ""),
                    'quantity': info['quantity'],
                    'tracking_number': info['tracking_number'],
                    'status': info['status']
                })
                # Set the overall order status based on the last line item's tracking status
                order_info['status'] = info['status']

        order_end_time = time.time()
        print(f"Processed order {order.order_number} in {order_end_time - order_start_time:.2f} seconds")
        return order_info
    except Exception as e:
        print(f"Error processing order {order.order_number}: {e}")
        return None

@app.route('/pending')
def pending_orders():
    # Use data_lock to read the global state safely
    with data_lock:
        current_orders = order_details
    
    all_orders = []
    pending_items_dict = {}
    
    for shopify_order in current_orders:
        if not shopify_order:
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
    """Fetches and processes recent Shopify orders (last few months)."""
    start_date = (datetime.now() - datetime.timedelta(days=90)).isoformat() # Look back 90 days
    
    new_order_details = []
    total_start_time = time.time()
    try:
        # Fetching latest 250 orders, ordered by creation date descending
        orders = shopify.Order.find(limit=250, order="created_at DESC", created_at_min=start_date)
    except Exception as e:
        print(f"Error fetching initial orders: {e}")
        return []
    
    async with aiohttp.ClientSession() as session:
        while True:
            # Note: limited_request is removed here as it's now handled by the outer scheduler thread for simplicity
            tasks = [process_order(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing an order: {result}")
                elif result is not None:
                    new_order_details.append(result)
            
            try:
                if not orders.has_next_page():
                    break
                orders = orders.next_page()
                # Enforce a delay between pages to respect API rate limits
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"Error fetching next page: {e}")
                break
    
    total_end_time = time.time()
    print(f"Processed {len(new_order_details)} orders in {total_end_time - total_start_time:.2f} seconds")
    return new_order_details

def adjust_to_shopify_timezone(from_date, to_date):
    # Parse input dates (assumed to be in YYYY-MM-DD format)
    from_date_obj = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
    to_date_obj = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    # Format the dates with explicit GMT+5 offset (Pakistan Standard Time)
    from_date_gmt_plus_5 = from_date_obj.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    to_date_gmt_plus_5 = to_date_obj.strftime('%Y-%m-%dT%H:%M:%S+05:00')
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
                await asyncio.sleep(1.0) # Respect rate limits
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
    
    from_date_utc, to_date_utc = adjust_to_shopify_timezone(from_date, to_date)
    print(f"Fetching from {from_date_utc} to {to_date_utc}")
    
    orders = asyncio.run(getShopifyOrderswithDates(from_date_utc, to_date_utc))
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
    tag = data.get('tag')
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"
    
    try:
        order = shopify.Order.find(order_id)
        if not hasattr(order, 'tags'):
            raise ValueError("Could not fetch a valid order. Ensure the order ID is correct.")
            
        # Special logic for cancellation/closing
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
            # Trigger immediate background refresh to update the global list
            scheduler.add_job(run_data_refresh, 'date') 
            return jsonify({"success": True, "message": "Tag applied successfully and refresh scheduled."})
        else:
            return jsonify({"success": False, "error": "Failed to save order changes."})
            
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route("/")
def tracking():
    with data_lock:
        current_orders = order_details
    return render_template("track_alk.html", order_details=current_orders)

def format_date(date_str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
    return date_obj.strftime("%Y-%m-%d")

def run_data_refresh():
    """Synchronous wrapper to run the asynchronous data load function for the scheduler."""
    global order_details
    print("--- Starting Background Order Refresh Job ---")
    try:
        new_orders = asyncio.run(getShopifyOrders())
        with data_lock:
            order_details = new_orders # Update the global list safely
        print(f"--- Background Refresh Complete. Loaded {len(order_details)} orders. ---")
    except Exception as e:
        print(f"--- Background Refresh Failed: {e} ---")

@app.route('/refresh', methods=['POST'])
def refresh_data():
    """Triggers the background refresh job immediately."""
    try:
        # Schedule a one-off job to run immediately in the background thread
        scheduler.add_job(run_data_refresh, 'date', run_date=datetime.now())
        return jsonify({
            'message': 'Data refresh job scheduled successfully (202 Accepted). Please wait a few moments and refresh the dashboard page.'
        }), 202
    except Exception as e:
        print(f"Error scheduling refresh: {e}")
        return jsonify({'message': 'Failed to schedule refresh'}), 500

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
    with data_lock:
        current_orders = order_details
    return render_template("undelivered.html", order_details=current_orders)

@app.route('/report')
def report():
    with data_lock:
        current_orders = order_details
    return render_template("report.html", order_details=current_orders)

@app.route("/track")
def tracking_track():
    with data_lock:
        current_orders = order_details
    return render_template("track_alk.html", order_details=current_orders)

def verify_shopify_webhook(request):
    """Verify the webhook using HMAC with the shared secret."""
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
        
        # Handle closed/archived orders
        if order_data.get('closed_at'):
            print(f"Order {order_shopify_id} is closed. Attempting removal from list.")
            with data_lock:
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
        
        if updated_order_info is None:
            return jsonify({'success': False, 'error': f'Failed to process order {order_shopify_id}'}), 500
            
        order_num_to_match = updated_order_info.get('order_num')
        
        # Update the global list safely
        with data_lock:
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
            'message': f'Order {order_num_to_match} processed successfully via webhook',
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
    
    with data_lock:
        current_orders = order_details
        
        # 1. Search by Shopify Order Number
        found_order = next((o for o in current_orders if o.get('order_num') == scan_term), None)
        
        # 2. If not found, search by Tracking Number
        if not found_order:
            for order in current_orders:
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

    # Trigger immediate background refresh after bulk tagging
    scheduler.add_job(run_data_refresh, 'date')
    
    return jsonify({
        'success': True,
        'tag_applied': final_tag,
        'total_orders': len(order_ids_to_tag),
        'results': results
    }), 200

# --- APPLICATION STARTUP ---

shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')

# Set Shopify credentials (keep this outside the if __name__ == "__main__" for WSGI)
try:
    shopify.ShopifyResource.set_site(shop_url)
    shopify.ShopifyResource.set_user(api_key)
    shopify.ShopifyResource.set_password(password)
except Exception as e:
    print(f"Shopify configuration failed: {e}")

# Initial load and Periodic refresh setup
# Start the scheduler and the initial job once
# The job will run every 60 minutes
scheduler.add_job(run_data_refresh, 'interval', minutes=60, id='shopify_data_refresh', replace_existing=True)
scheduler.start()

if __name__ == "__main__":
    # In local development, force an initial synchronous load for immediate UI data.
    if not order_details:
         print("Running initial load sync for local development...")
         run_data_refresh()
         print("Initial load finished.")
         
    # Render deployment solution: Use '0.0.0.0' and the PORT environment variable
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
