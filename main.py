import asyncio
import base64
import hashlib
import hmac
import os
import smtplib
import time
import json
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, flash, redirect, url_for, abort
from datetime import datetime
import pymssql, shopify
import aiohttp
import lazop
from tenacity import retry, stop_after_attempt, wait_exponential
from aiohttp import ClientTimeout, ClientSession, ClientError, BasicAuth
import pytz
from flask import send_file, make_response
import io
import functools

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')
pre_loaded = 0
order_details = []

# NOTE: Global semaphore removed to fix "different event loop" error.
# It is now handled dynamically inside 'limited_request'.

# PostEx Token
POSTEX_TOKEN = "M2E4Y2QyZTJiMjM0NGNjNGI4Y2E1YWYzNDY3MjE1ODY6MjFiOTFkNjVmZTNlNDMyNWI3MzNkYTU4NTM1OTQ3NmU="
POSTEX_BASE_URL = "https://api.postex.pk/services/integration/api"

POSTEX_ADDRESS_CODE = None


# --- RATE LIMIT DEFENDER (Sync) ---
def shopify_api_retry(func):
    """
    Decorator to handle Shopify 429 Too Many Requests errors
    for SYNCHRONOUS library calls.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        retries = 3
        while retries > 0:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_msg = str(e).lower()
                # Check for rate limit indicators in the error message
                if "429" in error_msg or "too many requests" in error_msg:
                    print(f"⚠️ Shopify Rate Limit Hit (Sync). Sleeping 2s... (Retries left: {retries})")
                    time.sleep(2 + random.uniform(0, 1))  # Add jitter
                    retries -= 1
                else:
                    raise e
        return func(*args, **kwargs)

    return wrapper


# --- Shopify Fulfillment Helper (Fixed for API 2025-01 & Rate Limits) ---
@shopify_api_retry
def fulfill_order_sync(order_id, tracking_number):
    try:
        # 1. Find Fulfillment Order
        fulfillment_orders = shopify.FulfillmentOrders.find(order_id=order_id)
        target_fo = next((fo for fo in fulfillment_orders if fo.status == 'open'), None)

        if not target_fo:
            print(f"Skipping fulfillment for {order_id}: No open fulfillment order.")
            return False

        # 2. Construct Payload
        payload = {
            "fulfillment": {
                "message": "Fulfilled via PostEx Integration",
                "notify_customer": True,
                "tracking_info": {
                    "number": tracking_number,
                    "url": f"https://postex.pk/tracking?cn={tracking_number}",
                    "company": "PostEx"
                },
                "line_items_by_fulfillment_order": [
                    {
                        "fulfillment_order_id": target_fo.id
                    }
                ]
            }
        }

        # 3. Request Settings
        url = "/admin/api/2025-01/fulfillments.json"
        headers = {"Content-Type": "application/json"}

        # 4. SEND REQUEST
        response = shopify.ShopifyResource.connection.post(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers
        )

        # 5. Check Success
        if response.code == 201:
            print(f"SUCCESS: Order {order_id} fulfilled. Tracking: {tracking_number}")
            return True
        else:
            print(f"FAILURE: Shopify returned code {response.code} for {order_id}.")
            return False

    except Exception as e:
        print(f"Shopify Fulfillment Error for {order_id}: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response Body: {e.response.body}")
        if "429" in str(e):
            raise e
        return False


async def get_pickup_address_code():
    global POSTEX_ADDRESS_CODE
    if POSTEX_ADDRESS_CODE:
        return POSTEX_ADDRESS_CODE

    url = f"{POSTEX_BASE_URL}/order/v1/get-merchant-address"
    headers = {'token': POSTEX_TOKEN}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('dist') and len(data['dist']) > 0:
                        POSTEX_ADDRESS_CODE = data['dist'][0].get('addressCode')
                        return POSTEX_ADDRESS_CODE
        except Exception as e:
            print(f"Error fetching PostEx address code: {e}")
    return None


async def fetch_postex_cities():
    url = f"{POSTEX_BASE_URL}/order/v2/get-operational-city"
    headers = {'token': POSTEX_TOKEN}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    cities = [c['operationalCityName'] for c in data.get('dist', [])]
                    return sorted(cities)
                return []
        except Exception as e:
            print(f"Error fetching PostEx cities: {e}")
            return []


@app.route('/prepare_postex_booking', methods=['POST'])
def prepare_booking():
    raw_ids = request.form.get('order_ids')
    if not raw_ids:
        return "No orders selected", 400

    selected_ids = json.loads(raw_ids)
    postex_cities = asyncio.run(fetch_postex_cities())
    orders_to_book = []

    for order_id_str in selected_ids:
        try:
            target_id = int(order_id_str)
        except:
            continue

        order_data = next((o for o in order_details if o['order_id'] == target_id), None)

        if order_data:
            is_paid = order_data.get('financial_status', '').lower() in ['paid', 'partially_refunded']
            cod_amount = 0.0 if is_paid else float(order_data.get('total_price', 0))

            shopify_city = order_data['customer_details'].get('city', '').strip()
            matched_city = ""

            for pc in postex_cities:
                if pc.lower() == shopify_city.lower():
                    matched_city = pc
                    break

            if not matched_city:
                for pc in postex_cities:
                    if shopify_city.lower() in pc.lower():
                        matched_city = pc
                        break

            orders_to_book.append({
                'order_id': order_data['order_id'],
                'order_num': order_data['order_num'],
                'customer_name': order_data['customer_details']['name'],
                'customer_phone': order_data['customer_details']['phone'],
                'address': order_data['customer_details']['address'],
                'shopify_city': shopify_city,
                'matched_city': matched_city,
                'cod_amount': int(cod_amount)
            })

    return render_template('booking.html', orders=orders_to_book, postex_cities=postex_cities)


@app.route('/submit_postex_booking', methods=['POST'])
def submit_booking():
    data = request.get_json()
    bookings = data.get('bookings', [])

    global POSTEX_ADDRESS_CODE
    if not POSTEX_ADDRESS_CODE:
        POSTEX_ADDRESS_CODE = asyncio.run(get_pickup_address_code())

    if not POSTEX_ADDRESS_CODE:
        return jsonify({'results': [], 'error': 'Could not fetch Pickup Address Code.'}), 500

    results = []

    async def book_order_async(booking_item):
        order_id = int(booking_item['order_id'])
        postex_city = booking_item['postex_city']
        cod_amount = booking_item['cod_amount']

        original_order = next((o for o in order_details if o['order_id'] == order_id), None)
        if not original_order:
            return {'order_id': order_id, 'success': False, 'message': 'Original data not found'}

        payload = {
            "orderRefNumber": str(original_order['order_num']),
            "invoicePayment": str(cod_amount),
            "customerName": original_order['customer_details']['name'],
            "customerPhone": original_order['customer_details']['phone'] or "03000000000",
            "deliveryAddress": original_order['customer_details']['address'],
            "cityName": postex_city,
            "invoiceDivision": 1,
            "items": 1,
            "orderType": "Normal",
            "transactionNotes": "Urgent Delivery",
            "pickupAddressCode": POSTEX_ADDRESS_CODE
        }

        url = f"{POSTEX_BASE_URL}/order/v3/create-order"
        headers = {
            'token': POSTEX_TOKEN,
            'Content-Type': 'application/json'
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(url, headers=headers, json=payload) as response:
                    resp_data = await response.json()

                    if response.status == 200 and resp_data.get('statusCode') == '200':
                        tracking = resp_data.get('dist', {}).get('trackingNumber', 'N/A')

                        original_order['status'] = 'CONSIGNMENT BOOKED'
                        original_order['tracking_number'] = tracking

                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, fulfill_order_sync, order_id, tracking)

                        return {'order_id': order_id, 'success': True, 'tracking': tracking}
                    else:
                        msg = resp_data.get('statusMessage', 'Unknown Error')
                        return {'order_id': order_id, 'success': False, 'message': msg}

            except Exception as e:
                return {'order_id': order_id, 'success': False, 'message': str(e)}

    async def process_all():
        tasks = [book_order_async(item) for item in bookings]
        return await asyncio.gather(*tasks)

    results = asyncio.run(process_all())

    return jsonify({'results': results})


@app.route('/print_labels')
def print_labels():
    tracking_numbers = request.args.get('tracking_numbers')
    if not tracking_numbers:
        return "No tracking numbers provided", 400

    url = f"{POSTEX_BASE_URL}/order/v1/get-invoice?trackingNumbers={tracking_numbers}"
    headers = {'token': POSTEX_TOKEN}

    async def fetch_pdf():
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
                return None

    pdf_content = asyncio.run(fetch_pdf())

    if pdf_content:
        response = make_response(pdf_content)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = 'inline; filename=airway_bills.pdf'
        return response
    else:
        return "Failed to fetch PDF from PostEx", 500


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=10))
async def fetch_with_retry(session, url, method="GET", **kwargs):
    async with session.request(method, url, **kwargs) as response:
        if response.status == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            print(f"Async Rate limit hit. Retrying after {retry_after} seconds...")
            await asyncio.sleep(retry_after)
            response.raise_for_status()

        if response.status == 404:
            print(f"Warning: Resource not found (404) at URL: {url}")
            return None

        response.raise_for_status()
        return await response.json()


# --- FIX FOR SEMAPHORE ERROR: DYNAMIC BINDING ---
async def limited_request(coroutine):
    """
    Ensure requests adhere to rate limits using a loop-bound semaphore.
    This fixes the 'bound to a different event loop' error.
    """
    loop = asyncio.get_running_loop()

    # Check if the current loop already has a semaphore attached to it
    if not hasattr(loop, 'shopify_sem'):
        # If not, create one specifically for this loop
        loop.shopify_sem = asyncio.Semaphore(2)

    async with loop.shopify_sem:
        await asyncio.sleep(0.5)
        return await coroutine


async def async_shopify_fetch(session, resource_path):
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    API_VERSION = '2024-04'

    base_url_clean = shop_url.split('/admin')[0].rstrip('/')
    shopify_url = f"{base_url_clean}/admin/api/{API_VERSION}/{resource_path.lstrip('/')}"
    auth = BasicAuth(api_key, password)

    return await limited_request(
        fetch_with_retry(session, shopify_url, auth=auth, headers={'Content-Type': 'application/json'})
    )


@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    to_emails = data.get('to', [])
    cc_emails = data.get('cc', [])
    subject = data.get('subject', '')
    body = data.get('body', '')

    try:
        smtp_server = 'smtp.gmail.com'
        smtp_port = 587
        smtp_user = os.getenv('SMTP_USER')
        smtp_password = os.getenv('SMTP_PASSWORD')

        msg = MIMEText(body)
        msg['From'] = smtp_user
        msg['To'] = ', '.join(to_emails)
        msg['Cc'] = ', '.join(cc_emails)
        msg['Subject'] = subject

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
    timeout = ClientTimeout(total=100)
    try:
        async with session.get(url, timeout=timeout) as response:
            if response.status == 200:
                data = await response.json()
                if isinstance(data, list) and data:
                    return data
                elif isinstance(data, dict) and data.get('d'):
                    return data['d']
                return []
            else:
                return {"error": f"HTTP {response.status}"}
    except Exception as e:
        return {"error": str(e)}


async def process_line_item(session, line_item, fulfillments):
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
                    if data and isinstance(data, list) and data[-1].get('ProcessDescForPortal'):
                        # Assumes the last item in the list holds the current status
                        tracking_details = data[-1]['ProcessDescForPortal']
                    else:
                        tracking_details = "DELIVERED"  # Default or fallback
                    tracking_info.append({
                        'tracking_number': tracking_number,
                        'status': tracking_details,
                        'quantity': item.quantity
                    })
    return tracking_info if tracking_info else [
        {"tracking_number": "N/A", "status": "Un-Booked", "quantity": line_item.quantity}
    ]


async def process_order(session, order):
    try:
        order_start_time = time.time()
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

        tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        results = await asyncio.gather(*tasks)

        for tracking_info_list, line_item in zip(results, order.line_items):
            if tracking_info_list is None: continue

            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = line_item.variant_title or ""

            if line_item.product_id is not None:
                try:
                    product_endpoint = f"products/{line_item.product_id}.json"
                    product_data = await async_shopify_fetch(session, product_endpoint)
                    product = product_data.get('product') if product_data else None

                    if product and product.get('variants'):
                        for variant in product['variants']:
                            if variant['id'] == line_item.variant_id:
                                image_id = variant.get('image_id')
                                variant_name = line_item.variant_title or ""
                                if image_id is not None:
                                    image_endpoint = f"products/{line_item.product_id}/images/{image_id}.json"
                                    image_data = await async_shopify_fetch(session, image_endpoint)
                                    if image_data and image_data.get('image'):
                                        image_src = image_data['image']['src']
                                else:
                                    image_src = product.get('image', {}).get('src', image_src)
                                break
                except Exception as e:
                    print(f"Error fetching product details: {e}")

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
        return order_info
    except Exception as e:
        print(f"Error processing order {order.order_number}: {e}")
        return None


@app.route('/pending')
def pending_orders():
    all_orders = []
    pending_items_dict = {}
    global order_details

    for shopify_order in order_details:
        if not shopify_order: continue

        if shopify_order.get('status') in ['CONSIGNMENT BOOKED', 'Un-Booked', 'Booked / Fulfilled']:
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
                'order_link': shopify_order['order_link'],
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
                status = item['status']

                if product_title not in pending_items_dict:
                    pending_items_dict[product_title] = {
                        'item_image': item_image,
                        'item_title': product_title,
                        'quantity': 0,
                        'statuses': {}
                    }
                pending_items_dict[product_title]['quantity'] += quantity
                current_statuses = pending_items_dict[product_title]['statuses']
                current_statuses[status] = current_statuses.get(status, 0) + quantity

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

                time.sleep(1.0)
                orders = orders.next_page()

            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    total_end_time = time.time()
    print(f"Processed {len(order_details)} orders in {total_end_time - total_start_time:.2f} seconds")
    return order_details


def adjust_to_shopify_timezone(from_date, to_date):
    from_date = datetime.strptime(from_date, "%Y-%m-%d").replace(hour=0, minute=0, second=0)
    to_date = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    from_date_gmt_plus_5 = from_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    to_date_gmt_plus_5 = to_date.strftime('%Y-%m-%dT%H:%M:%S+05:00')
    return from_date_gmt_plus_5, to_date_gmt_plus_5


async def getShopifyOrderswithDates(start_date: str, end_date: str):
    order_details = []

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
                    print(f"Error processing: {result}")
                elif result is not None:
                    order_details.append(result)

            try:
                if not orders.has_next_page():
                    break

                time.sleep(1.0)
                orders = orders.next_page()
            except Exception as e:
                print(f"Error fetching next page: {e}")
                break

    return order_details


@app.route('/fetch-orders', methods=['POST'])
def fetch_orders():
    data = request.get_json()
    from_date = data.get('fromDate')
    to_date = data.get('toDate')
    from_date_utc, to_date_utc = adjust_to_shopify_timezone(from_date, to_date)

    orders = asyncio.run(getShopifyOrderswithDates(from_date_utc, to_date_utc))
    total_sales = 0.0
    for order in orders:
        try:
            total_sales += float(order['total_price'])
        except:
            pass

    return jsonify({
        'orders': orders,
        'total_sales': total_sales,
        'total_cost': 0.0
    })


@app.route('/apply_tag', methods=['POST'])
@shopify_api_retry
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    tag = data.get('tag')

    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"
    try:
        order = shopify.Order.find(order_id)
        if not hasattr(order, 'tags'):
            raise ValueError("Could not fetch a valid order.")

        if tag.strip().lower() == "returned":
            order.cancel()
        if tag.strip().lower() == "delivered":
            order.close()

        tags = [t.strip() for t in order.tags.split(", ")] if order.tags else []
        if "Leopards Courier" in tags: tags.remove("Leopards Courier")
        if tag_with_date not in tags: tags.append(tag_with_date)

        order.tags = ", ".join(tags)

        if order.save():
            return jsonify({"success": True, "message": "Tag applied successfully."})
        else:
            return jsonify({"success": False, "error": "Failed to save order changes."})

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/apply_bulk_tag', methods=['POST'])
def apply_bulk_tag():
    data = request.json
    order_ids_to_tag = data.get('order_ids', [])
    tag_type = data.get('tag_type')

    if not order_ids_to_tag or tag_type not in ['RETURNED', 'DISPATCHED']:
        return jsonify({"success": False, "error": "Invalid input."}), 400

    today_date = datetime.now().strftime('%Y-%m-%d')
    results = []

    for order_shopify_id in order_ids_to_tag:
        try:
            time.sleep(0.6)

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
                results.append({'id': order_shopify_id, 'status': 'failed', 'message': 'Failed to save.'})

        except Exception as e:
            if "429" in str(e):
                time.sleep(2)
                results.append({'id': order_shopify_id, 'status': 'error', 'message': 'Rate Limit Hit'})
            else:
                results.append({'id': order_shopify_id, 'status': 'error', 'message': str(e)})

    return jsonify({
        'success': True,
        'tag_applied': final_tag,
        'total_orders': len(order_ids_to_tag),
        'results': results
    }), 200


@app.route("/")
def tracking_home():
    global order_details
    return render_template("track_alk.html", order_details=order_details)


@app.route('/refresh', methods=['POST'])
def refresh_data():
    global order_details
    try:
        order_details = asyncio.run(getShopifyOrders())
        return render_template("track_alk.html", order_details=order_details)
    except Exception as e:
        print(f"Error refreshing data: {e}")
        return jsonify({'message': 'Failed to refresh data'}), 500


@app.route('/track/<tracking_num>')
def displayTracking(tracking_num):
    async def async_func():
        async with aiohttp.ClientSession() as session:
            return await fetch_tracking_data(session, tracking_num)

    data = asyncio.run(async_func())
    return render_template('trackingdata_alk.html', data=data)


@app.route('/undelivered')
def undelivered():
    global order_details
    return render_template("undelivered.html", order_details=order_details)


@app.route('/report')
def report():
    global order_details
    return render_template("report.html", order_details=order_details)


def verify_shopify_webhook(request):
    shopify_hmac = request.headers.get('X-Shopify-Hmac-Sha256')
    data = request.get_data()
    secret = os.getenv('SHOPIFY_WEBHOOK_SECRET')
    if secret is None: return False
    digest = hmac.new(secret.encode('utf-8'), data, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode('utf-8')
    return hmac.compare_digest(computed_hmac, shopify_hmac)


@app.route('/shopify/webhook/order_updated', methods=['POST'])
def shopify_order_updated():
    global order_details
    try:
        if not verify_shopify_webhook(request):
            return jsonify({'error': 'Invalid webhook signature'}), 401

        order_data = request.get_json()
        order_shopify_id = order_data.get('id')
        if not order_shopify_id: return jsonify({'error': 'No order id'}), 400

        if order_data.get('closed_at'):
            order_details[:] = [o for o in order_details if o.get('order_id') != order_shopify_id]
            return jsonify({'success': True}), 200

        order = shopify.Order.find(order_shopify_id)
        if not order: return jsonify({'error': 'Not found'}), 404

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

        return jsonify({'success': True}), 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({'success': False}), 500


@app.route('/scanner')
def scanner_page():
    return render_template('scanner.html')


@app.route('/api/scan/order', methods=['POST'])
def scan_single_order():
    scanned_value = request.json.get('scan_input')
    if not scanned_value: return jsonify({'error': 'No input'}), 400
    scan_term = str(scanned_value).strip().replace("#", "")

    found_order = next((o for o in order_details if o.get('order_num') == scan_term), None)
    if not found_order:
        for order in order_details:
            if order.get('line_items'):
                for item in order['line_items']:
                    if item.get('tracking_number') == scan_term:
                        found_order = order;
                        break
                if found_order: break

    if found_order:
        items_list = [{'title': item['product_title'], 'quantity': item['quantity'], 'image_src': item['image_src']} for
                      item in found_order['line_items']]
        return jsonify({'success': True, 'order_num': found_order['order_num'], 'items': items_list}), 200
    else:
        return jsonify({'success': False, 'error': 'Not found'}), 404


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)

try:
    print("Starting initial fetch...")
    order_details = asyncio.run(getShopifyOrders())
    print(f"Loaded {len(order_details)} orders.")
except Exception as e:
    print(f"Init load failed: {e}")
    order_details = []

if __name__ == "__main__":
    app.run(port=5001)
