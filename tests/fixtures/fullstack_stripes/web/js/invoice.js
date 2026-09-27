function renderInvoiceTotal(data) {
  $('#invoice-total').text(Acme.formatCents(data.amount));
}

function loadInvoice(invoiceId) {
  $.getJSON('/vendor/Invoice.action?load=&invoiceId=' + invoiceId, renderInvoiceTotal);
}

$(document).ready(function () {
  var invoiceId = $('input[name="invoiceId"]').val();
  if (invoiceId) {
    loadInvoice(invoiceId);
  }
  $('.invoice-save').on('click', function () {
    $('.invoice-form').addClass('invoice-form--busy');
  });
});
