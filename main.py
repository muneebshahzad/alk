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

app = Flask(__name__)
app.debug = True
app.secret_key = os.getenv('APP_SECRET_KEY', 'default_secret_key')  # Use environment variable
pre_loaded = 0
order_details = []



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


async def fetch_tracking_data(session, tracking_number):
    url = f"https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory?cn={tracking_number}"

    try:
        async with session.get(url) as response:
            if response.status == 200:
                return await response.json()
            else:
                print(f"Error: HTTP {response.status} for tracking number {tracking_number}")
                return {"error": f"HTTP {response.status}"}
    except aiohttp.ClientError as e:
        print(f"Request failed: {e}")
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
                    try:
                        data = await fetch_tracking_data(session, tracking_number)
                    except Exception as e:
                        print(f"Error fetching tracking data for {tracking_number}: {e}")
                        data = None

                    if data:
                        try:
                            tracking_details = data[-1].get('ProcessDescForPortal', 'N/A')
                        except Exception as e:
                            print(f"Error parsing tracking details: {e}")
                            tracking_details = "N/A"
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
    order_start_time = time.time()

    try:
        created_at = order.created_at
        parsed_datetime = datetime.fromisoformat(created_at[:-6])
        formatted_datetime = parsed_datetime.strftime("%b %d, %Y")
    except Exception as e:
        print(f"Error parsing created_at for order {order.id}: {e}")
        formatted_datetime = "Unknown Date"

    status = (getattr(order, 'fulfillment_status', 'Un-fulfilled') or "Un-fulfilled").title()

    customer_details = {
        "name": getattr(order.billing_address, "name", "N/A"),
        "address": getattr(order.billing_address, "address1", "N/A"),
        "city": getattr(order.billing_address, "city", "N/A"),
        "phone": getattr(order.billing_address, "phone", "N/A")
    }

    order_info = {
        'order_id': getattr(order, 'order_number', 'Unknown'),
        'tracking_id': 'N/A',
        'created_at': formatted_datetime,
        'total_price': getattr(order, 'total_price', 0.0),
        'line_items': [],
        'financial_status': getattr(order, 'financial_status', 'Unknown').title(),
        'fulfillment_status': status,
        'customer_details': customer_details,
        'tags': (getattr(order, 'tags', "") or "").split(", "),
        'id': getattr(order, 'id', 'Unknown')
    }

    tasks = [process_line_item(session, line_item, getattr(order, 'fulfillments', [])) for line_item in getattr(order, 'line_items', [])]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for tracking_info_list, line_item in zip(results, getattr(order, 'line_items', [])):
        if isinstance(tracking_info_list, Exception):
            print(f"Error processing line item for order {order.id}: {tracking_info_list}")
            continue

        try:
            product_id = getattr(line_item, 'product_id', None)
            if product_id:
                product = shopify.Product.find(product_id)
                variant_name = ""
                image_src = "https://static.thenounproject.com/png/1578832-200.png"
                if product and product.variants:
                    for variant in product.variants:
                        if variant.id == line_item.variant_id:
                            if variant.image_id:
                                images = shopify.Image.find(image_id=variant.image_id, product_id=product_id)
                                variant_name = getattr(line_item, 'variant_title', '')
                                for image in images:
                                    if image.id == variant.image_id:
                                        image_src = image.src
            else:
                image_src = "https://static.thenounproject.com/png/1578832-200.png"
        except Exception as e:
            print(f"Error retrieving product information: {e}")
            image_src = "https://static.thenounproject.com/png/1578832-200.png"
            variant_name = ""

        for info in tracking_info_list or []:
            order_info['line_items'].append({
                'fulfillment_status': getattr(line_item, 'fulfillment_status', 'Unknown'),
                'image_src': image_src,
                'product_title': f"{line_item.title} - {variant_name}".strip(),
                'quantity': info.get('quantity', 0),
                'tracking_number': info.get('tracking_number', 'N/A'),
                'status': info.get('status', 'Unknown')
            })
            order_info['status'] = info['status']


    order_end_time = time.time()
    print(f"Processed order {order_info['customer_details']['name']} ({order.id}) in {order_end_time - order_start_time:.2f} seconds")
    return order_info

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




@app.route('/apply_tag', methods=['POST'])
def apply_tag():
    data = request.json
    order_id = data.get('order_id')
    tag = data.get('tag')

    # Get today's date in YYYY-MM-DD format
    today_date = datetime.now().strftime('%Y-%m-%d')
    tag_with_date = f"{tag.strip()} ({today_date})"

    try:
        # Fetch the order
        order = shopify.Order.find(order_id)

        # If the tag is "Returned", cancel the order
        if tag.strip().lower() == "returned":
            # Attempt to cancel the order
            if order.cancel():
                print("Order Cancelled")
            else:
                print("Order Cancellation Failed")
        if tag.strip().lower() == "delivered":
            if order.close():
                print("Order Cloed")
            else:
                print("Order Closing Failed")

        # Process existing tags
        if order.tags:
            tags = [t.strip() for t in order.tags.split(", ")]  # Remove excess spaces
        else:
            tags = []

        if "Leopards Courier" in tags:
            print("LEOPARDS")
            tags.remove("Leopards Courier")

        # Add new tag if it doesn't already exist
        if tag_with_date not in tags:
            tags.append(tag_with_date)

        # Update the order with the new tags
        order.tags = ", ".join(tags)

        # Save the order
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



@app.route('/pending')
def pending_orders():
    all_orders = []
    pending_items_dict = {}  # Dictionary to track quantities of each unique item

    global order_details
    print(order_details[0])

    # Process Shopify orders with the specified statuses
    for shopify_order in order_details:
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
                'tracking_number': shopify_order['tracking_id'],
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

@app.route('/undelivered')
def undelivered():
    global order_details, pre_loaded
    return render_template("undelivered.html", order_details=order_details)


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

