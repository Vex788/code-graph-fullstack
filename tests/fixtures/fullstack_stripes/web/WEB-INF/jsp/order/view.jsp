<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<stripes:form beanclass="com.acme.web.action.OrderActionBean" class="order-form">
  <stripes:hidden name="orderId"/>
  <stripes:submit name="place" value="Place order"/>
</stripes:form>
<div class="order-total">${actionBean.total}</div>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
