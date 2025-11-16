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
import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout
from aiohttp import ClientSession, ClientError
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
import pytz

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')  # Use environment variable
pre_loaded = 0
order_details = []
semaphore = asyncio.Semaphore(2)


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
        smtp_user = os.getenv('SMTP_USER')  # Use environment variable
        smtp_password = os.getenv('SMTP_PASSWORD')  # Use environment variable

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


async def limited_request(coroutine):
    """Ensure requests adhere to rate limits."""
    async with semaphore:
        await asyncio.sleep(0.5)  # Enforce delay for Shopify rate limits
        return await coroutine


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
                    if data:
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
        created_at_str = order.created_at  # Assuming it's in the format '2025-01-07T23:42:49+05:00'

        # Parse the string into a datetime object
        created_at_obj = datetime.fromisoformat(created_at_str)

        # Format the datetime object to 'YYYY-MM-DD'
        formatted_date = created_at_obj.strftime('%Y-%m-%d')

        order_info = {
            'order_link': "https://admin.shopify.com/store/alkaramat/orders/" + str(order.id),
            'order_num': order.name.replace("#", ""),  # Used for display and matching in webhooks
            'order_id': order.id,  # The unique numeric Shopify ID
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

            # Use default image and variant name initially
            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = line_item.variant_title or ""

            # Check if image fetching is necessary
            if line_item.product_id is not None:
                try:
                    # Fetch product and check for cached data or default
                    product = shopify.Product.find(line_item.product_id)
                    if product and product.variants:
                        for variant in product.variants:
                            if variant.id == line_item.variant_id:
                                if variant.image_id is not None:
                                    images = shopify.Image.find(image_id=variant.image_id,
                                                                product_id=line_item.product_id)
                                    variant_name = line_item.variant_title
                                    for image in images:
                                        if image.id == variant.image_id:
                                            image_src = image.src
                                else:
                                    variant_name = ""
                                    image_src = product.image.src if product.image else image_src
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
                order_info['status'] = info['status']

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
    start_date = datetime(2024, 9, 1).isoformat()
    order_details = []
    total_start_time = time.time()

    try:
        orders = shopify.Order.find(limit=250, order="created_at DESC", created_at_min=start_date)
    except Exception as e:
        print(f"Error fetching orders: {e}")
        return []

    async with aiohttp.ClientSession() as session:
        while True:
            tasks = [limited_request(process_order(session, order)) for order in orders]
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


def adjust_to_shopify_timezone(from_date, to_date):
    # Parse input dates (assumed to be in GMT+5 already)
    from_date = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
    to_date = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    # Format the dates with explicit GMT+5 offset
    from_date_gmt_plus_5 = from_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    to_date_gmt_plus_5 = to_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')

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
            # Use the process_order function for consistency and simplicity (it no longer calculates cost)
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

    # Ensure the fromDate and toDate are passed correctly (format: YYYY-MM-DD)
    from_date = data.get('fromDate')
    to_date = data.get('toDate')

    # Adjust the dates to Shopify's timezone (convert to UTC)
    from_date_utc, to_date_utc = adjust_to_shopify_timezone(from_date, to_date)
    print(f"Fetching from {from_date_utc} to {to_date_utc}")

    # Now pass the adjusted UTC dates to fetch orders
    orders = asyncio.run(getShopifyOrderswithDates(from_date_utc, to_date_utc))

    total_sales = 0.0
    for order in orders:
        # Cost feature removed, only calculate sales
        try:
            total_sales += float(order['total_price'])
        except (TypeError, ValueError):
            print(f"Warning: Could not parse total_price for order {order.get('order_num', 'N/A')}")
            pass

    # Return the orders with totals
    return jsonify({
        'orders': orders,
        'total_sales': total_sales,
        'total_cost': 0.0  # Total cost removed/set to zero
    })


# REMOVED: process_shopify_order_with_details function is no longer needed/redundant with process_order

# REMOVED: fetch_line_item_cost_and_tracking function is no longer needed


@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    print(order_id)
    tag = data.get('tag')

    # Get today's date in YYYY-MM-DD format
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"
    try:
        # Fetch the order
        order = shopify.Order.find(order_id)

        # Ensure it's a single order
        if not hasattr(order, 'tags'):
            raise ValueError("Could not fetch a valid order. Ensure the order ID is correct.")

        # If the tag is "Returned", cancel the order
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

        # Process existing tags
        tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []

        if "Leopards Courier" in tags:
            tags.remove("Leopards Courier")

        if tag_with_date not in tags:
            tags.append(tag_with_date)

        # Update and save the order
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
    # Parse the date string
    date_obj = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %z")
    # Format the date object to only show the date
    return date_obj.strftime("%Y-%m-%d")


@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details
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
    print(f"Tracking Number: {tracking_num}")  # Debug line

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
    # Ensure order_details is a list of dictionaries (or the desired structure)
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
    data = request.get_data()  # Raw request body (bytes)

    # Retrieve the secret from environment variables
    secret = os.getenv('SHOPIFY_WEBHOOK_SECRET')

    # Check that the secret is set. If not, fail verification (or raise error)
    if secret is None:
        print("SECURITY ALERT: SHOPIFY_WEBHOOK_SECRET is not set.")
        return False  # Fail verification if secret is not set

    # Compute the HMAC digest and base64 encode it.
    digest = hmac.new(
        secret.encode('utf-8'),
        data,
        hashlib.sha256
    ).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')

    # Compare the computed HMAC with the one sent by Shopify.
    return hmac.compare_digest(computed_hmac, shopify_hmac)


@app.route('/shopify/webhook/order_updated', methods=['POST'])
def shopify_order_updated():
    global order_details
    try:
        # **WEBHOOK DEBUG FIX: 1. Verify HMAC**
        if not verify_shopify_webhook(request):
            print("Webhook verification failed (HMAC mismatch or no secret).")
            return jsonify({'error': 'Invalid webhook signature'}), 401

        order_data = request.get_json()
        order_shopify_id = order_data.get('id')
        if not order_shopify_id:
            return jsonify({'error': 'No order id found in payload'}), 400

        print(f"Received webhook for order Shopify ID: {order_shopify_id}")

        # **WEBHOOK DEBUG FIX: 2. Handle closed/archived orders**
        # The order_id from the webhook payload is the numeric Shopify ID.
        if order_data.get('closed_at'):
            print(f"Order {order_shopify_id} is closed. Attempting removal from list.")
            # Find the order in the list by its numeric 'order_id'
            order_details[:] = [o for o in order_details if o.get('order_id') != order_shopify_id]
            return jsonify({'success': True, 'message': f'Order {order_shopify_id} removed.'}), 200

        # **WEBHOOK DEBUG FIX: 3. Fetch and process the updated order**
        # Use the raw Shopify API to get the current state
        order = shopify.Order.find(order_shopify_id)
        if not order:
            return jsonify({'error': f'Order {order_shopify_id} not found'}), 404

        async def update_order():
            async with aiohttp.ClientSession() as session:
                # Use the main processing function
                return await process_order(session, order)

        updated_order_info = asyncio.run(update_order())

        # The 'order_num' is the human-readable order name (e.g., #1001) used for matching in your list
        order_num_to_match = updated_order_info.get('order_num')

        # **WEBHOOK DEBUG FIX: 4. Update the global list by matching on 'order_num'**
        updated = False
        for idx, existing_order in enumerate(order_details):
            # Match using the human-readable 'order_num' since that's what process_order returns
            if existing_order.get('order_num') == order_num_to_match:
                order_details[idx] = updated_order_info
                updated = True
                break
        if not updated:
            # If it's a new order or the first time it's been processed, append it
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

import shopify # Make sure this is imported at the top

@app.route('/api/apply_bulk_tag', methods=['POST'])
def apply_bulk_tag():
    data = request.json
    order_ids_to_tag = data.get('order_ids', []) # List of Shopify numeric IDs
    tag_type = data.get('tag_type') # Expected: 'RETURNED' or 'DISPATCHED'

    if not order_ids_to_tag or tag_type not in ['RETURNED', 'DISPATCHED']:
        return jsonify({"success": False, "error": "Invalid input or tag type."}), 400

    today_date = datetime.now().strftime('%Y-%m-%d')
    results = []

    for order_shopify_id in order_ids_to_tag:
        try:
            # 1. Determine the correct tag name based on the type
            if tag_type == 'RETURNED':
                base_tag = "Return Received"
            elif tag_type == 'DISPATCHED':
                base_tag = "DISPATCHED" # Keeps the original DISPATCHED tag

            final_tag = f"{base_tag} ({today_date})"

            # 2. Fetch the order using the numeric ID
            order = shopify.Order.find(order_shopify_id)
            if not order:
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Order not found.'})
                continue

            # 3. Get existing tags
            tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []

            # Ensure the dated tag is unique before adding
            if final_tag not in tags:
                tags.append(final_tag)

            # 4. Save the order with the updated tags
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

shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)

# Only run initial load if outside the `if __name__ == "__main__"` block for Gunicorn/WSGI
# If you are running with `python app.py`, you might want this inside the main block.
# Keeping it outside for general Flask/WSGI setup compatibility.
try:
    order_details = asyncio.run(getShopifyOrders())
except Exception as e:
    print(f"Initial order loading failed: {e}")
    order_details = []


if __name__ == "__main__":
    # Environment variable check is redundant here but safe
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    app.run(port=5001)


