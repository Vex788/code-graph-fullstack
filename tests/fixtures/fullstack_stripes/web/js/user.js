function highlightUser(row) {
  $(row).toggleClass('user-row--active');
}

$(document).ready(function () {
  $('.user-row').on('click', function () {
    highlightUser(this);
  });
  $.get('/user/List.action', function (html) {
    $('.user-list').replaceWith($(html).find('.user-list'));
  });
});
