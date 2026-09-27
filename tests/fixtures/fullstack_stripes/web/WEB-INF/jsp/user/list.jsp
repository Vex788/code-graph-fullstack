<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<script src="/js/user.js"></script>
<stripes:form action="/user/List.action" class="user-form">
  <stripes:text name="user.name"/>
  <stripes:text name="user.email"/>
  <stripes:submit name="register" value="Register"/>
</stripes:form>
<ul class="user-list">
  <c:forEach items="${actionBean.users}" var="u">
    <li class="user-row">${u.name}</li>
  </c:forEach>
</ul>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
