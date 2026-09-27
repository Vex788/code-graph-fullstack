var Acme = window.Acme || {};

Acme.showError = function (message) {
  $('.page-header').append('<div class="error">' + message + '</div>');
};

Acme.formatCents = function (cents) {
  return (cents / 100).toFixed(2);
};
