package com.acme.web.action;

import com.acme.model.User;
import com.acme.service.UserService;
import java.util.List;
import net.sourceforge.stripes.action.DefaultHandler;
import net.sourceforge.stripes.action.ForwardResolution;
import net.sourceforge.stripes.action.HandlesEvent;
import net.sourceforge.stripes.action.RedirectResolution;
import net.sourceforge.stripes.action.Resolution;
import net.sourceforge.stripes.action.UrlBinding;
import net.sourceforge.stripes.integration.spring.SpringBean;
@UrlBinding("/user/List.action")
public class UserListActionBean extends BaseActionBean {

    @SpringBean
    private UserService userService;

    private User user;

    private List<User> users;

    public User getUser() {
        return user;
    }

    public void setUser(User user) {
        this.user = user;
    }

    public List<User> getUsers() {
        return users;
    }

    @DefaultHandler
    public Resolution list() {
        users = userService.listUsers();
        return new ForwardResolution("/WEB-INF/jsp/user/list.jsp");
    }

    @HandlesEvent("register")
    public Resolution register() {
        userService.register(user);
        return new RedirectResolution("/user/List.action");
    }
}
