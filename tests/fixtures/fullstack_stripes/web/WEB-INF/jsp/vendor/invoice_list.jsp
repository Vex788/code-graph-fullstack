<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<link rel="stylesheet" href="/css/invoice.css"/>
<table class="invoice-table">
  <c:forEach items="${actionBean.invoices}" var="inv">
    <tr>
      <td>${inv.vendorCode}</td>
      <td><stripes:link beanclass="com.acme.web.action.VendorInvoiceActionBean">
        <stripes:param name="invoiceId" value="${inv.id}"/>Open</stripes:link></td>
    </tr>
  </c:forEach>
</table>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
