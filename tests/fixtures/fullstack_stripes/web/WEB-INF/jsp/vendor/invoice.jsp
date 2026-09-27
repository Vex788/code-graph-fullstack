<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<link rel="stylesheet" href="/css/invoice.css"/>
<script src="/js/invoice.js"></script>
<h1>Vendor invoice</h1>
<stripes:form beanclass="com.acme.web.action.VendorInvoiceActionBean" class="invoice-form">
  <stripes:errors/>
  <stripes:hidden name="invoiceId"/>
  <label for="amount">Amount</label>
  <stripes:text id="amount" name="invoice.amount" class="invoice-amount"/>
  <stripes:text name="invoice.currency" class="invoice-currency"/>
  <input type="text" name="invoice.vendorCode" class="invoice-vendor"/>
  <stripes:submit name="save" value="Save" class="invoice-save"/>
</stripes:form>
<div id="invoice-total" class="invoice-total"></div>
<script type="text/javascript">
  var ctx = '${pageContext.request.contextPath}';
  $(function () {
    $('.invoice-form').on('submit', function (event) {
      event.preventDefault();
      $.post(ctx + '/vendor/Invoice.action', $(this).serialize(), function (data) {
        $('#invoice-total').text(data.amount);
      });
    });
  });
</script>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
