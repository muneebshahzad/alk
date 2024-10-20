    function sendEmail(type, trackingNumber) {
        let subject = '';
        let body = '';

        switch(type) {
            case 'delay':
                subject = 'Delay in Delivery';
                body = `Dear Team,\n\nPlease note that there has been significant delay in delivery of CN# ${trackingNumber}, Please arrange urgent delivery.\n\nThanks and Regards,\nTick Bags\nwww.alkaramat.com`;
                break;
            case 'incomplete':
                subject = 'FAKE DELIVERY REASON';
                body = `Dear Team,\n\nPlease note that consignee is waiting for delivery against CN# ${trackingNumber}, while the tracking status says incomplete address even though the address is 100% accurate and phone number is completely accessible. Please arrange urgent delivery and make sure this shipment is not returned.\n\nThanks and Regards,\nTick Bags\nwww.alkaramat.com`;
                break;
            case 'refusal':
                subject = 'URGENT - FAKE ORDER REFUSAL';
                body = `Dear Team,\n\nPlease note that consignee is waiting for delivery against CN# ${trackingNumber}, while the tracking status says that customer has refused the delivery. Please arrange urgent delivery and make sure this shipment is not returned.\n\nThanks and Regards,\nTick Bags\nwww.alkaramat.com`;
                break;
        }

        // Prepare data to send to backend
        let data = {
            to: ['cs@postex.pk','cs@callcourier.com.pk'],
            cc: ['muneeb.shahzad@hotmail.com'],
            subject: subject,
            body: body
        };

        // Make HTTP POST request to backend endpoint
        fetch('/send-email', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(data),
        })
        .then(response => {
            if (response.ok) {
                alert('Email sent successfully'); // Notify user
                console.log('Email sent successfully'); // Log success to console
            } else {
                alert('Failed to send email'); // Notify user of failure
                console.error('Failed to send email'); // Log failure to console
            }
        })
        .catch(error => {
            alert('Error sending email'); // Notify user of error
            console.error('Error:', error); // Log error to console
        });
    }
    function toggleStatusButtons(button) {
        var statusButtons = button.nextElementSibling;
        if (statusButtons.style.display === "none" || statusButtons.style.display === "") {
            statusButtons.style.display = "block";
        } else {
            statusButtons.style.display = "none";
        }
    }

   function applyTag(orderId, tag) {
    return fetch('/apply_tag', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ order_id: orderId, tag: tag })
    })
    .then(response => response.json())
    .then(data => {
        return { orderId: orderId, success: data.success }; // return result of this order
    })
    .catch(error => {
        console.error('Error:', error);
        return { orderId: orderId, success: false }; // return failed result
    });
}

function applyDeliveredToAll() {
    const orders = {{ order_details|tojson }};
    
    const deliveredOrders = orders.filter(order => order.status === 'delivered');
    
    const applyPromises = deliveredOrders.map(order => applyTag(order.id, 'Delivered'));
    
    // Wait for all tag applications to complete
    Promise.all(applyPromises)
        .then(results => {
            let successCount = 0;
            let failureCount = 0;

            results.forEach(result => {
                if (result.success) {
                    successCount++;
                } else {
                    failureCount++;
                }
            });

            // Display a single summary message after all requests
            alert(`Tag applied successfully to ${successCount} orders.\nFailed to apply to ${failureCount} orders.`);
        })
        .catch(error => {
            console.error('Error processing orders:', error);
            alert('An error occurred while processing the orders.');
        });
}

        document.getElementById('refreshButton').addEventListener('click', async function () {
             alert('Refreshing in Background!')
            const response = await fetch('/refresh', {
                method: 'POST'
            });
            const result = await response.json();
            if (result.message === 'Data refreshed successfully') {
                location.reload();
            } else {
                alert('Failed to refresh data');
            }
        });


function applyDeliveredToAll() {
    const orders = {{ order_details|tojson }};

    orders.forEach(order => {
        if (order.status === 'DELIVERED') {
            applyTag(order.id, 'Delivered');
        }
    });
}
