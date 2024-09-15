// Initialize Income Pie Chart
const incomeCtx = document.getElementById('incomePieChart').getContext('2d');
const incomePieChart = new Chart(incomeCtx, {
    type: 'pie',
    data: {
        labels: JSON.parse(document.getElementById('incomeData').getAttribute('data-labels')),
        datasets: [{
            data: JSON.parse(document.getElementById('incomeData').getAttribute('data-values')),
            backgroundColor: JSON.parse(document.getElementById('incomeData').getAttribute('data-colors')),
            hoverBackgroundColor: JSON.parse(document.getElementById('incomeData').getAttribute('data-colors')),
            hoverBorderColor: "rgba(234, 236, 244, 1)",
        }],
    },
    options: {
        maintainAspectRatio: false,
        tooltips: {
            backgroundColor: "rgb(255,255,255)",
            bodyFontColor: "#858796",
            borderColor: '#dddfeb',
            borderWidth: 1,
            xPadding: 15,
            yPadding: 15,
            displayColors: false,
            caretPadding: 10,
        },
        legend: {
            display: false
        },
        cutoutPercentage: 0,
    },
});

// Initialize Expense Pie Chart
const expenseCtx = document.getElementById('expensePieChart').getContext('2d');
const expensePieChart = new Chart(expenseCtx, {
    type: 'pie',
    data: {
        labels: JSON.parse(document.getElementById('expenseData').getAttribute('data-labels')),
        datasets: [{
            data: JSON.parse(document.getElementById('expenseData').getAttribute('data-values')),
            backgroundColor: JSON.parse(document.getElementById('expenseData').getAttribute('data-colors')),
            hoverBackgroundColor: JSON.parse(document.getElementById('expenseData').getAttribute('data-colors')),
            hoverBorderColor: "rgba(234, 236, 244, 1)",
        }],
    },
    options: {
        maintainAspectRatio: false,
        tooltips: {
            backgroundColor: "rgb(255,255,255)",
            bodyFontColor: "#858796",
            borderColor: '#dddfeb',
            borderWidth: 1,
            xPadding: 15,
            yPadding: 15,
            displayColors: false,
            caretPadding: 10,
        },
        legend: {
            display: false
        },
        cutoutPercentage: 0,
    },
});

// Validate Amount Input
function validateAmount(input) {
    const value = input.value;
    if (!/^\d*\.?\d*$/.test(value)) {
        input.value = value.slice(0, -1);
    }
}

// Fetch Expense Data
async function fetchExpenseData() {
    try {
        const response = await fetch('/expense_data');
        const data = await response.json();

        const typeSelect = document.getElementById('expenseType');
        const subtypeSelect = document.getElementById('expenseSubtype');

        // Populate expense types
        for (const type of data.types) {
            const option = document.createElement('option');
            option.value = type.expense_id;
            option.textContent = type.expense_title;
            typeSelect.appendChild(option);
        }

        // Store subtypes globally with expense_id as the key
        window.expenseSubtypes = data.subtypes;
    } catch (error) {
        console.error('Error fetching expense data:', error);
    }
}

// Populate Expense Subtypes
function populateExpenseSubtypes() {
    const typeId = document.getElementById('expenseType').value;
    const subtypeSelect = document.getElementById('expenseSubtype');
    subtypeSelect.innerHTML = '';

    if (typeId && window.expenseSubtypes[typeId]) {
        for (const subtype of window.expenseSubtypes[typeId]) {
            const option = document.createElement('option');
            option.value = subtype;
            option.textContent = subtype;
            subtypeSelect.appendChild(option);
        }
    }
}

document.addEventListener('DOMContentLoaded', fetchExpenseData);

// Fetch Income Data
async function fetchIncomeData() {
    try {
        const response = await fetch('/income_data');
        const data = await response.json();

        const typeSelect = document.getElementById('incomeType');
        const subtypeSelect = document.getElementById('incomeSubtype');

        // Populate income types
        for (const type of data.types) {
            const option = document.createElement('option');
            option.value = type.income_id;
            option.textContent = type.income_title;
            typeSelect.appendChild(option);
        }

        // Store subtypes globally with income_id as the key
        window.incomeSubtypes = {};
        for (const subtype of data.subtypes) {
            if (!window.incomeSubtypes[subtype.income_id]) {
                window.incomeSubtypes[subtype.income_id] = [];
            }
            window.incomeSubtypes[subtype.income_id].push(subtype.subtype_title);
        }
    } catch (error) {
        console.error('Error fetching income data:', error);
    }
}

// Populate Income Subtypes
function populateIncomeSubtypes() {
    const typeId = document.getElementById('incomeType').value;
    const subtypeSelect = document.getElementById('incomeSubtype');
    subtypeSelect.innerHTML = '';

    if (typeId && window.incomeSubtypes[typeId]) {
        for (const subtype of window.incomeSubtypes[typeId]) {
            const option = document.createElement('option');
            option.value = subtype;
            option.textContent = subtype;
            subtypeSelect.appendChild(option);
        }
    }
}

document.addEventListener('DOMContentLoaded', fetchIncomeData);

// Handle Income Form Submission
$(document).ready(function() {
    $('#incomeForm').on('submit', function(event) {
        event.preventDefault();

        var incomeTypeSelect = $('#incomeType');
        var incomeTypeTitle = incomeTypeSelect.find('option:selected').text();
        var incomeSubtypeSelect = $('#incomeSubtype');
        var incomeSubtypeTitle = incomeSubtypeSelect.find('option:selected').text();

        var formData = {
            amount: $('#incomeAmount').val(),
            income_type: incomeTypeTitle,
            income_subtype: incomeSubtypeTitle,
            description: $('#incomeDescription').val()
        };

        $.ajax({
            url: '/add_income',
            type: 'POST',
            data: formData,
            success: function(response) {
                alert(response.message);
                $('#incomeAmount').val('');
                $('#incomeDescription').val('');
            },
            error: function(xhr, status, error) {
                console.log('Error: ' + error);
                alert('An error occurred while adding income.');
            }
        });
    });

    // Handle Expense Form Submission
    $('#expenseForm').on('submit', function(event) {
        event.preventDefault();

        var expenseTypeSelect = $('#expenseType');
        var expenseTypeTitle = expenseTypeSelect.find('option:selected').text();
        var expenseSubtypeSelect = $('#expenseSubtype');
        var expenseSubtypeTitle = expenseSubtypeSelect.find('option:selected').text();

        var formData = {
            amount: $('#expenseAmount').val(),
            expense_type: expenseTypeTitle,
            expense_subtype: expenseSubtypeTitle,
            description: $('#expenseDescription').val()
        };

        $.ajax({
            url: '/add_expense',
            type: 'POST',
            data: formData,
            success: function(response) {
                alert(response.message);
                $('#expenseAmount').val('');
                $('#expenseDescription').val('');
            },
            error: function(xhr, status, error) {
                console.log('Error: ' + error);
                alert('An error occurred while adding expense.');
            }
        });
    });
});

// Fetch and Display Payables
function fetchPayables() {
    fetch('/get_payables')
        .then(response => response.json())
        .then(data => {
            console.log('Response data:', data);

            let payablesList = document.getElementById('payables-list');
            let totalPayablesElement = document.getElementById('total-payables');

            if (totalPayablesElement) {
                totalPayablesElement.textContent = data.total_payables;
            } else {
                console.error('Total payables element not found.');
            }

            payablesList.innerHTML = '';
            data.payables.forEach(item => {
                let amountWithoutRs = item.amount.replace('Rs ', '');
                let row = `
                    <tr>
                        <td>${item.id}</td>
                        <td>${item.date}</td>
                        <td>${item.description}</td>
                        <td>${amountWithoutRs}</td>
                    </tr>
                `;
                payablesList.insertAdjacentHTML('beforeend', row);
            });
        })
        .catch(error => console.error('Error fetching payables:', error));
}

document.addEventListener('DOMContentLoaded', fetchPayables);
