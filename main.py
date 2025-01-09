<!DOCTYPE html>
<html lang="en">

<head>

    <meta charset="utf-8">
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
    <meta name="description" content="">
    <meta name="author" content="">

    <title>Daily Tasker - Tracking</title>

    <!-- Custom fonts for this template -->
    <link href="static/assets/vendor/fontawesome-free/css/all.min.css" rel="stylesheet" type="text/css">
    <link
        href="https://fonts.googleapis.com/css?family=Nunito:200,200i,300,300i,400,400i,600,600i,700,700i,800,800i,900,900i"
        rel="stylesheet">

    <!-- Custom styles for this template -->
    <link href="static/assets/css/sb-admin-2.min.css" rel="stylesheet">

    <!-- Custom styles for this page -->
    <link href="static/assets/vendor/datatables/dataTables.bootstrap4.min.css" rel="stylesheet">
 <style>
        .status-buttons {
            display: none;
            margin-top: 10px;
        }
        .status-buttons .btn {
            margin-right: 5px;
            margin-bottom: 5px;
        }
    </style>
</head>

<body id="page-top">

    <!-- Page Wrapper -->
    <div id="wrapper">

        <!-- Sidebar -->
        {% include 'header_alk_track.html' %}
        <!-- End of Sidebar -->

        <!-- Content Wrapper -->
        <div id="content-wrapper" class="d-flex flex-column">

            <!-- Main Content -->
            <div id="content">

                <!-- Topbar -->
                <nav class="navbar navbar-expand navbar-light bg-white topbar mb-4 static-top shadow">

                    <!-- Sidebar Toggle (Topbar) -->
                    <form class="form-inline">
                        <button id="sidebarToggleTop" class="btn btn-link d-md-none rounded-circle mr-3">
                            <i class="fa fa-bars"></i>
                        </button>
                    </form>

                    <!-- Topbar Search -->


                    <!-- Topbar Navbar -->
                    <ul class="navbar-nav ml-auto">

                        <!-- Nav Item - Search Dropdown (Visible Only XS) -->
                        <li class="nav-item dropdown no-arrow d-sm-none">
                            <a class="nav-link dropdown-toggle" href="#" id="searchDropdown" role="button" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
                                <i class="fas fa-search fa-fw"></i>
                            </a>
                            <!-- Dropdown - Messages -->
                            <div class="dropdown-menu dropdown-menu-right p-3 shadow animated--grow-in" aria-labelledby="searchDropdown">
                                <form class="form-inline mr-auto w-100 navbar-search">
                                    <div class="input-group">
                                        <input type="text" class="form-control bg-light border-0 small" placeholder="Search for..." aria-label="Search" aria-describedby="basic-addon2">
                                        <div class="input-group-append">
                                            <button class="btn btn-primary" type="button">
                                                <i class="fas fa-search fa-sm"></i>
                                            </button>

                                        </div>
                                    </div>
                                </form>
                            </div>
                        </li>
                         <div class="topbar-divider d-none d-sm-block"></div>

                        <!-- Nav Item - User Information -->
                        <li class="nav-item dropdown no-arrow">
                            <a class="nav-link dropdown-toggle" href="#" id="userDropdown" role="button" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
                                <span class="mr-2 d-none d-lg-inline text-gray-600 small">Al-Karamat</span>
                                <img class="img-profile rounded-circle" src="static/img/undraw_profile.svg">
                            </a>
                            <!-- Dropdown - User Information -->
                            <div class="dropdown-menu dropdown-menu-right shadow animated--grow-in" aria-labelledby="userDropdown">
                                <a class="dropdown-item" href="#">
                                    <i class="fas fa-user fa-sm fa-fw mr-2 text-gray-400"></i>
                                    Profile
                                </a>
                                <a class="dropdown-item" href="#">
                                    <i class="fas fa-cogs fa-sm fa-fw mr-2 text-gray-400"></i>
                                    Settings
                                </a>
                                <a class="dropdown-item" href="#">
                                    <i class="fas fa-list fa-sm fa-fw mr-2 text-gray-400"></i>
                                    Activity Log
                                </a>
                                <div class="dropdown-divider"></div>
                                <a class="dropdown-item" href="#" data-toggle="modal" data-target="#logoutModal">
                                    <i class="fas fa-sign-out-alt fa-sm fa-fw mr-2 text-gray-400"></i>
                                    Logout
                                </a>
                            </div>
                        </li>

                    </ul>

                </nav>
                <!-- End of Topbar -->

                <!-- Begin Page Content -->
                <div class="container-fluid">


                    <!-- Page Heading -->
                    <h1 class="h3 mb-2 text-gray-800">Order Tracking</h1>

                    <div class="row mb-4">
    <div class="col-md-2">
        <div class="card text-white bg-primary">
            <div class="card-body">
                <h5 class="card-title">All</h5>
                <p>Total Value: Rs <span id="all-value">0</span></p>
                <p>Total Quantity: <span id="all-quantity">0</span></p>
            </div>
        </div>
    </div>
    <div class="col-md-2">
        <div class="card text-white bg-success">
            <div class="card-body">
                <h5 class="card-title">Dispatch</h5>
                <p>Value: Rs <span id="dispatch-value">0</span></p>
                <p>Quantity: <span id="dispatch-quantity">0</span></p>
            </div>
        </div>
    </div>
    <div class="col-md-2">
        <div class="card text-white bg-warning">
            <div class="card-body">
                <h5 class="card-title">Booked</h5>
                <p>Value: Rs <span id="booked-value">0</span></p>
                <p>Quantity: <span id="booked-quantity">0</span></p>
            </div>
        </div>
    </div>
    <div class="col-md-2">
        <div class="card text-white bg-danger">
            <div class="card-body">
                <h5 class="card-title">Delivered</h5>
                <p>Value: Rs <span id="delivered-value">0</span></p>
                <p>Quantity: <span id="delivered-quantity">0</span></p>
            </div>
        </div>
    </div>
    <div class="col-md-2">
        <div class="card text-white bg-info">
            <div class="card-body">
                <h5 class="card-title">Returns</h5>
                <p>Value: Rs <span id="returns-value">0</span></p>
                <p>Quantity: <span id="returns-quantity">0</span></p>
            </div>
        </div>
    </div>
    <div class="col-md-2">
        <div class="card text-white bg-secondary">
            <div class="card-body">
                <h5 class="card-title">Shipped</h5>
                <p>Value: Rs <span id="shipped-value">0</span></p>
                <p>Quantity: <span id="shipped-quantity">0</span></p>
            </div>
        </div>
    </div>
</div>


                    <!-- DataTales Example -->
                    <div class="card shadow mb-4">
                        <div class="card-header py-3">
                            <h6 class="m-0 font-weight-bold text-primary">Shopify - Pending </h6>
                        </div>
                        <div class="card-body">
                            <div class="table-responsive">

                                <table class="table table-bordered" id="dataTable" width="100%" cellspacing="0">

    <thead>

        <tr>
            <th>Order ID</th>
            <th style="width: 15%;">Customer</th>
            <th>City</th>
            <th>Tags</th>
            <th>Payment</th>
            <th>Date</th>
            <th>Total Price</th>
            <th>Items</th>
            <th>Status</th>
            <th>Actions</th>
        </tr>
    </thead>
    <tfoot>
        <tr>
            <th>Order ID</th>
            <th style="width: 15%;">Customer</th>
            <th>City</th>
            <th>Tags</th>
            <th>Payment</th>
            <th>Date</th>
            <th>Total Price</th>
            <th>Items</th>
            <th>Status</th>
            <th>Actions</th>
        </tr>
    </tfoot>

    <tbody>
        {% for order in order_details %}
            <tr>
                        <td>{{ order.order_num }}</td>
        <td>
            {% if order.customer_details %}
                {{ order.customer_details.name or "Unknown" }}<br>
                {{ order.customer_details.address or "Not provided" }}<br>
                {{ order.customer_details.city or "Unknown" }}<br>
                {{ order.customer_details.phone or "Not provided" }}<br>
                {% if order.customer_details.phone %}
                    <a href="tel:{{ order.customer_details.phone }}" class="btn btn-primary btn-sm">Call</a>
                    <div class="btn-group">
                        <button type="button" class="btn btn-success btn-sm dropdown-toggle" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
                            WhatsApp
                        </button>
                        <div class="dropdown-menu">
                            <a class="dropdown-item" href="https://wa.me/{{ order.customer_details.phone }}?text=Hello%20{{ order.customer_details.name }},%20Your%20order%23{{ order.order_id }}%20from%20AlchemyOfDecor.com%20has%20been%20dispatched%20and%20is%20expected%20to%20be%20delivered%20in%203-7%20working%20days.%20Your%20patience%20is%20highly%20appreciated." target="_blank">Send Dispatch Message</a>
                            <a class="dropdown-item" href="https://wa.me/{{ order.customer_details.phone }}?text=Hello%20{{ order.customer_details.name }},%20Your%20order%23{{ order.order_id }}%20from%20AlchemyOfDecor.com%20has%20been%20delayed,%20please%20be%20assured%20that%20we%20are%20trying%20our%20best%20to%20deliver%20it%20at%20earliest.%20Your%20patience%20is%20highly%20appreciated." target="_blank">Send Delay Message</a>
                            <a class="dropdown-item" href="https://wa.me/{{ order.customer_details.phone }}?text=Hello%20{{ order.customer_details.name }},%20Logistics%20company%20made%20an%20attempt%20to%20deliver%20your%20order%23{{ order.order_id }}%20from%20AlchemyOfDecor.com%20but%20as%20per%20them,%20you%20weren't%20available%20at%20address%20or%20the%20address%20was%20closed.%20Please%20let%20us%20know%20when%20should%20we%20re-arrange%20the%20delivery.%20Thank%20You" target="_blank">Send Address Closed Message</a>
                            <a class="dropdown-item" href="https://wa.me/{{ order.customer_details.phone }}?text=Hello%20{{ order.customer_details.name }},%20Logistics%20company%20made%20an%20attempt%20to%20deliver%20your%20order%23{{ order.order_id }}%20from%20AlchemyOfDecor.com%20and%20according%20to%20them,%20you%20refused%20to%20accept%20the%20delivery.%20Please%20let%20us%20know%20if%20you're%20still%20interested%20in%20receiving%20the%20product." target="_blank">Send Refused Message</a>
                            <a class="dropdown-item" href="https://wa.me/{{ order.customer_details.phone }}?text=Hello%20{{ order.customer_details.name }},%20Your%20order%23{{ order.order_id }}%20from%20AlchemyOfDecor.com%20was%20delivered%20recently.%20Please%20let%20us%20know%20about%20your%20experience%20with%20us.%20Your%20feedback%20will%20surely%20help%20us%20improve.%20Thank%20You!" target="_blank">Send Feedback Message</a>
                        </div>
                    </div>
                {% else %}
                    <p>Phone number not available</p>
                {% endif %}
            {% else %}
                <p>Customer details not available</p>
            {% endif %}
        </td>
        <td>{{ order.customer_details.city or "Unknown" if order.customer_details else "Unknown" }}</td>


                <td>{% for tag in order.tags %}
                     <span class="badge badge-secondary">{{ tag }}</span><br>
                {% endfor %}</td>
                <td>{{ order.financial_status }}</td>
                <td>{{ order.created_at }}</td>
                <td>{{ order.total_price }}</td>
                <td>
                    <table class="table table-sm">
                        <thead>
                            <tr>
                                <th>Image</th>
                                <th style="width: 30%;">Title</th>
                                <th>Pcs</th>
                                <th>Tracking Number</th>
                                <th style="width: 20%;">Status</th>
                                <th>Email</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for item in order.line_items %}
                                <tr>
                                    <td><img src="{{ item.image_src }}" width="50" height="50"></td>
                                    <td>{{ item.product_title }}</td>
                                    <td>{{ item.quantity }}</td>
                                    <td><a href=track/{{item.tracking_number}} target=”_blank”> {{ item.tracking_number }} </a> </td>
                                    <td>{{ item.status }}</td>

                                    <td>
                                       <div class="btn-group">
    <button type="button" class="btn btn-info btn-sm dropdown-toggle" data-toggle="dropdown" aria-haspopup="true" aria-expanded="false">
        Email
    </button>
    <div class="dropdown-menu">
        <a class="dropdown-item" href="#" onclick="sendEmail('delay', '{{ item.tracking_number }}')">Send Email for Delay</a>
        <a class="dropdown-item" href="#" onclick="sendEmail('incomplete', '{{ item.tracking_number }}')">Send Email for Incomplete Address</a>
        <a class="dropdown-item" href="#" onclick="sendEmail('refusal', '{{ item.tracking_number }}')">Send Email for Fake Refusal</a>
        <a class="dropdown-item" href="#" onclick="sendEmail('refusal', '{{ item.tracking_number }}')">Send Custom Email</a>
    </div>
</div>
                                    </td>

                                </tr>
                            {% endfor %}
                        </tbody>

                    </table>
                </td>
                 <td> {{ order.status}}</td>
                 <td>
                        <button class="btn btn-primary btn-sm" onclick="toggleStatusButtons(this)">Mark As</button>
                        <div class="status-buttons">
                             <button class="btn btn-outline-success btn-sm" onclick="applyTag('{{ order.order_id }}', 'Delivered')">Delivered</button>
                            <button class="btn btn-outline-secondary btn-sm" onclick="applyTag('{{ order.order_id }}', 'Re-Dispatch')">Re-Dispatch</button>
                            <button class="btn btn-outline-secondary btn-sm" onclick="applyTag('{{ order.order_id }}', 'Wants to Open Parcel')">Wants to Open Parcel</button>
                            <button class="btn btn-outline-danger btn-sm" onclick="applyTag('{{ order.order_id }}', 'Returned')">Returned</button>
                            <button class="btn btn-outline-warning btn-sm" onclick="applyTag('{{ order.order_id }}', 'Cancelled')">Cancelled</button>
                            <button class="btn btn-outline-info btn-sm" onclick="applyTag('{{ order.order_id }}', 'Confirmed')">Confirmed</button>
                            <button class="btn btn-outline-secondary btn-sm" onclick="applyTag('{{ order.order_id }}', 'Call Not Attended')">Call Not Attended</button>
                        </div>
                    </td>
            </tr>
        {% endfor %}
    </tbody>
</table>
                                    <button class="btn btn-primary btn-sm" onclick="applyDeliveredToAll()">Apply 'Delivered' to All Delivered Orders</button>
                            </div>
                        </div>
                    </div>

                </div>
                <!-- /.container-fluid -->

            </div>
            <!-- End of Main Content -->

            <!-- Footer -->
            <footer class="sticky-footer bg-white">
                <div class="container my-auto">
                    <div class="copyright text-center my-auto">
                        <span>Copyright &copy; Your Website 2020</span>
                    </div>
                </div>
            </footer>
            <!-- End of Footer -->

        </div>
        <!-- End of Content Wrapper -->

    </div>
    <!-- End of Page Wrapper -->

    <!-- Scroll to Top Button-->
    <a class="scroll-to-top rounded" href="#page-top">
        <i class="fas fa-angle-up"></i>
    </a>

    <!-- Logout Modal-->
    <div class="modal fade" id="logoutModal" tabindex="-1" role="dialog" aria-labelledby="exampleModalLabel"
        aria-hidden="true">
        <div class="modal-dialog" role="document">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title" id="exampleModalLabel">Ready to Leave?</h5>
                    <button class="close" type="button" data-dismiss="modal" aria-label="Close">
                        <span aria-hidden="true">×</span>
                    </button>
                </div>
                <div class="modal-body">Select "Logout" below if you are ready to end your current session.</div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" type="button" data-dismiss="modal">Cancel</button>
                    <a class="btn btn-primary" href="login.html">Logout</a>
                </div>
            </div>
        </div>
    </div>

    <!-- Bootstrap core JavaScript-->
    <script src="static/assets/vendor/jquery/jquery.min.js"></script>
    <script src="static/assets/vendor/bootstrap/js/bootstrap.bundle.min.js"></script>

    <!-- Core plugin JavaScript-->
    <script src="static/assets/vendor/jquery-easing/jquery.easing.min.js"></script>

    <!-- Custom scripts for all pages-->
    <script src="static/assets/js/sb-admin-2.min.js"></script>

    <!-- Page level plugins -->
    <script src="static/assets/vendor/datatables/jquery.dataTables.min.js"></script>
    <script src="static/assets/vendor/datatables/dataTables.bootstrap4.min.js"></script>

    <!-- Page level custom scripts -->
    <script src="static/assets/js/demo/datatables-demo.js"></script>
        <script src="static/assets/js/demo/datatables-demo.js"></script>
    <script src="static/assets/js/track.js"></script>
<script>
    function applyTag(orderId, tag) {
    console.log("Order ID:", orderId);
    if (!orderId) {
        alert("Order ID is missing");
        return;
    }
    fetch('/apply_tag', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ order_id: orderId, tag: tag })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            alert("Tag applied successfully.");
        } else {
            alert("Error: " + data.error);
        }
    })
    .catch(error => console.error('Error:', error));
}
</script>
<script>
   $(document).ready(function() {
    // Destroy existing DataTable instance if present
    if ($.fn.DataTable.isDataTable('#dataTable')) {
        $('#dataTable').DataTable().destroy();
    }

    // Initialize the DataTable with sorting on the first column in descending order
    $('#dataTable').DataTable({
        "order": [[0, 'desc']],  // Sort by the first column (index 0) in descending order
        "columnDefs": [
            { "searchable": false, "targets": [-1, 4] }  // Make the last column and the 5th column non-searchable
        ],
        "initComplete": function(settings, json) {
            console.log('DataTable initialized');
        },
        "drawCallback": function(settings) {
            console.log('Sorting applied: ', settings.aaSorting);
        }
    });
});

    function applyDeliveredToAll() {
    const orders = {{ order_details|tojson }};

    orders.forEach(order => {
        if (order.status === 'DELIVERED') {
            applyTag(order.order_id, 'Delivered');
        }
    });
}
</script>
<script>
        $(document).ready(function() {
            var currentUrl = window.location.pathname;
            $('.nav-item a').each(function() {
                var linkUrl = $(this).attr('href');
                if (currentUrl === '/' + linkUrl) {
                    $(this).parent().addClass('active');
                }
            });
        });
    </script>

<script>
    // Function to calculate and update values for the cards
function updateOrderStats(orderDetails) {
    let allValue = 0, allQuantity = 0;
    let dispatchValue = 0, dispatchQuantity = 0;
    let bookedValue = 0, bookedQuantity = 0;
    let deliveredValue = 0, deliveredQuantity = 0;
    let returnsValue = 0, returnsQuantity = 0;

    orderDetails.forEach(order => {
        let orderValue = parseFloat(order.total_price) || 0;
        let orderQuantity = order.line_items.reduce((sum, item) => sum + item.quantity, 0);

        allValue += orderValue;
        allQuantity += orderQuantity;

        switch (order.status) {
            case 'PICKED FROM SHIPPER':
                dispatchValue += orderValue;
                dispatchQuantity += orderQuantity;
                break;
            case 'CONSIGNMENT BOOKED':
                bookedValue += orderValue;
                bookedQuantity += orderQuantity;
                break;
            case 'DELIVERED':
                deliveredValue += orderValue;
                deliveredQuantity += orderQuantity;
                break;
            default:
                if (order.status.includes('RETURN')) {
                    returnsValue += orderValue;
                    returnsQuantity += orderQuantity;
                }
        }
    });

    let shippedValue = allValue - (dispatchValue + bookedValue + deliveredValue + returnsValue);
    let shippedQuantity = allQuantity - (dispatchQuantity + bookedQuantity + deliveredQuantity + returnsQuantity);

    // Update card values in the DOM
    document.getElementById('all-value').innerText = allValue.toFixed(2);
    document.getElementById('all-quantity').innerText = allQuantity;
    document.getElementById('dispatch-value').innerText = dispatchValue.toFixed(2);
    document.getElementById('dispatch-quantity').innerText = dispatchQuantity;
    document.getElementById('booked-value').innerText = bookedValue.toFixed(2);
    document.getElementById('booked-quantity').innerText = bookedQuantity;
    document.getElementById('delivered-value').innerText = deliveredValue.toFixed(2);
    document.getElementById('delivered-quantity').innerText = deliveredQuantity;
    document.getElementById('returns-value').innerText = returnsValue.toFixed(2);
    document.getElementById('returns-quantity').innerText = returnsQuantity;
    document.getElementById('shipped-value').innerText = shippedValue.toFixed(2);
    document.getElementById('shipped-quantity').innerText = shippedQuantity;
}

// Call the function with order details fetched from the server
updateOrderStats({{ order_details|tojson }});

</script>

</body>

</html>
