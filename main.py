import asyncio
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
            'order_num': order.name.replace("#",""),
            'order_id': order.id,
            'created_at': formatted_date,
            'total_price': order.total_price,
            'line_items': [],
            'financial_status': order.financial_status.title(),
            'fulfillment_status': order.fulfillment_status or "Unfulfilled",
            'customer_details': {
                "name": getattr(order.billing_address, "name", " "),
                "address": getattr(order.billing_address, "address1", " "),
                "city": getattr(order.billing_address, "city", " "),
                "phone": getattr(order.billing_address, "phone", " ")
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
        if shopify_order['status'] in ['CONSIGNMENT BOOKED', 'Un-Booked']:
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
                'order_via': 'Shopify',
                'order_id': shopify_order['order_id'],
                'status': shopify_order['status'],
                'tracking_number': shopify_order.get('tracking_number', 'N/A'),
                'date': shopify_order['created_at'],
                'items_list': shopify_items_list
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
        orders = shopify.Order.find(limit=5, order="created_at DESC", created_at_min=start_date)
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
                else:
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


from datetime import datetime, timedelta
import pytz


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
            limit=250,
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
            tasks = [process_shopify_order_with_details(session, order) for order in orders]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    print(f"Error processing an order: {result}")
                else:
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

    # Ensure the fromDate and toDate are passed correctly (format: YYYY-MM-DD)
    from_date = data.get('fromDate')
    to_date = data.get('toDate')

    # Adjust the dates to Shopify's timezone (convert to UTC)
    from_date_utc, to_date_utc = adjust_to_shopify_timezone(from_date, to_date)
    print(from_date_utc)
    print(to_date_utc)
    # Now pass the adjusted UTC dates to fetch orders
    orders = asyncio.run(getShopifyOrderswithDates(from_date_utc, to_date_utc))

    total_sales = 0.0
    total_cost = 0.0
    for order in orders:
        total_sales += float(order['total_price'])  # Assuming total_price is a string, convert to float
        total_cost += float(order['total_item_cost'])  # Assuming total_item_cost is a string, convert to float

    # Return the orders with totals
    return jsonify({
        'orders': orders,
        'total_sales': total_sales,
        'total_cost': total_cost
    })


async def process_shopify_order_with_details(session, order):
    try:
        # Ensure required keys are present in the order
        if not hasattr(order, 'order_number') or not hasattr(order, 'line_items'):
            raise ValueError(f"Order {order.id} is missing critical attributes.")

        # Prepare order information
        order_info = {
            'order_id': order.order_number,
            'created_at': order.created_at,
            'total_price': order.total_price or "0.0",  # Default to 0.0 if missing
            'line_items': [],
            'financial_status': order.financial_status.title() if order.financial_status else "Unknown",
            'fulfillment_status': order.fulfillment_status or "Unfulfilled",
            'total_item_cost': 0,  # To track the total cost of the items
            'tracking_info': [],  # For all tracking details
            'status': "Unfulfilled",  # Default status
        }

        # Process line items with tracking and cost details
        tasks = [process_line_item(session, line_item, order.fulfillments) for line_item in order.line_items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for line_item, tracking_info_list in zip(order.line_items, results):
            if isinstance(tracking_info_list, Exception):
                print(f"Error processing line item {line_item.id}: {tracking_info_list}")
                continue

            # Initialize placeholders for image, variant details, and cost
            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = ""
            line_item_cost = 0.0  # Initialize cost for this line item

            # Fetch product and variant details for the line item
            if line_item.product_id is not None:
                try:
                    product = shopify.Product.find(line_item.product_id)
                    if product and product.variants:
                        for variant in product.variants:
                            if variant.id == line_item.variant_id:
                                # Fetch variant-specific image
                                if variant.image_id:
                                    images = shopify.Image.find(image_id=variant.image_id,
                                                                product_id=line_item.product_id)
                                    for image in images:
                                        if image.id == variant.image_id:
                                            image_src = image.src
                                            break
                                else:
                                    # Fallback to product-level image
                                    image_src = product.image.src if product.image else image_src
                                variant_name = line_item.variant_title

                                # Fetch cost from inventory item
                                if hasattr(variant, "inventory_item_id"):
                                    inventory_item = shopify.InventoryItem.find(variant.inventory_item_id)
                                    if inventory_item and hasattr(inventory_item, "cost"):
                                        line_item_cost = float(inventory_item.cost) if inventory_item.cost else 0.0
                                break

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
                    'status': info['status'],
                    'cost': line_item_cost,  # Include the cost for the line item
                })

            # Update the total item cost for the order
            order_info['total_item_cost'] += line_item_cost * sum(info['quantity'] for info in tracking_info_list)

            # Update order-level status based on tracking details
            if tracking_info_list:
                order_info['status'] = tracking_info_list[-1]['status']  # Use the last tracking status

        return order_info

    except Exception as e:
        print(f"Failed to process order {getattr(order, 'order_number', 'Unknown')}: {e}")
        return None


async def fetch_line_item_cost_and_tracking(session, line_item, fulfillments):
    """
    Fetch the tracking information of a line item.

    Args:
        session (aiohttp.ClientSession): The HTTP session for API requests.
        line_item: A Shopify line item object.
        fulfillments: List of fulfillments related to the order.

    Returns:
        tuple: (tracking_info_list)
    """
    try:
        # Step 1: Initialize an empty tracking info list
        tracking_info_list = []

        # Step 2: Fetch tracking information
        if line_item.fulfillment_status == "fulfilled":
            for fulfillment in fulfillments:
                if fulfillment.status == "cancelled":
                    continue

                for item in fulfillment.line_items:
                    if item.id == line_item.id:
                        tracking_number = fulfillment.tracking_number
                        data = await fetch_tracking_data(session, tracking_number)
                        tracking_details = (
                            data[-1].get("ProcessDescForPortal", "DELIVERED") if data else "N/A"
                        )
                        tracking_info_list.append({
                            'tracking_number': tracking_number,
                            'status': tracking_details,
                            'quantity': item.quantity
                        })

        return tracking_info_list

    except Exception as e:
        print(f"Error fetching tracking for line item {getattr(line_item, 'id', 'Unknown')}: {e}")
        return []


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


shop_url = os.getenv('SHOP_URL')
api_key = os.getenv('API_KEY')
password = os.getenv('PASSWORD')
shopify.ShopifyResource.set_site(shop_url)
shopify.ShopifyResource.set_user(api_key)
shopify.ShopifyResource.set_password(password)
order_details = asyncio.run(getShopifyOrders())

if __name__ == "__main__":
    shop_url = os.getenv('SHOP_URL')
    api_key = os.getenv('API_KEY')
    password = os.getenv('PASSWORD')
    app.run(port=5001)
