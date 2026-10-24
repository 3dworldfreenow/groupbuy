/* Progressive enhancement only — every form works without JavaScript. */
(function () {
  "use strict";

  // Confirmation prompts: <form data-confirm="Are you sure?">
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // Loading state: disable the submit button to prevent double submission.
    var button = event.submitter || form.querySelector("[type=submit]");
    if (button && !form.hasAttribute("data-no-loading")) {
      window.setTimeout(function () {
        button.disabled = true;
        var text = button.getAttribute("data-loading-text") || "Working…";
        button.innerHTML = '<span class="spinner-border me-1" role="status" aria-hidden="true"></span>' + text;
      }, 0);
    }
  }, true);

  // Live order estimate on the "My order" page.
  function toNumber(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function formatMoney(n, symbol) {
    return symbol + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  var estimateForm = document.querySelector("[data-estimate-form]");
  if (estimateForm) {
    var symbol = estimateForm.getAttribute("data-currency") || "";
    var update = function () {
      var total = 0;
      estimateForm.querySelectorAll("[data-qty-input]").forEach(function (input) {
        var price = toNumber(input.getAttribute("data-price"));
        total += toNumber(input.value) * price;
      });
      var out = document.querySelector("[data-estimate-total]");
      if (out) { out.textContent = total > 0 ? formatMoney(total, symbol) : "—"; }
    };
    estimateForm.addEventListener("input", update);
    update();
  }

  // Bid form: live line totals and subtotal.
  var bidForm = document.querySelector("[data-bid-form]");
  if (bidForm) {
    var bidSymbol = bidForm.getAttribute("data-currency") || "";
    var homeOrders = toNumber(bidForm.getAttribute("data-home-orders"));
    var recalc = function () {
      var subtotal = 0;
      bidForm.querySelectorAll("[data-bid-row]").forEach(function (row) {
        var q = toNumber(row.querySelector("[data-bid-qty]").value);
        var p = toNumber(row.querySelector("[data-bid-price]").value);
        var line = q * p;
        subtotal += line;
        var cell = row.querySelector("[data-bid-line]");
        if (cell) { cell.textContent = formatMoney(line, bidSymbol); }
      });
      var fee = toNumber((bidForm.querySelector("[name=delivery_fee]") || {}).value);
      var home = toNumber((bidForm.querySelector("[name=home_delivery_fee]") || {}).value) * homeOrders;
      var s = bidForm.querySelector("[data-bid-subtotal]");
      var t = bidForm.querySelector("[data-bid-total]");
      if (s) { s.textContent = formatMoney(subtotal, bidSymbol); }
      if (t) { t.textContent = formatMoney(subtotal + fee + home, bidSymbol); }
    };
    bidForm.addEventListener("input", recalc);
    recalc();
  }

  // Copy-to-clipboard buttons: <button data-copy="text">
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () {
          var original = btn.innerHTML;
          btn.innerHTML = '<i class="bi bi-check2"></i> Copied';
          window.setTimeout(function () { btn.innerHTML = original; }, 1500);
        });
      }
    });
  });

  // Toggle visibility of address selector based on fulfilment choice.
  document.querySelectorAll("[data-fulfilment-choice]").forEach(function (group) {
    var target = document.querySelector(group.getAttribute("data-fulfilment-choice"));
    var sync = function () {
      var checked = group.querySelector("input:checked");
      if (target) { target.classList.toggle("d-none", !checked || checked.value !== "home"); }
    };
    group.addEventListener("change", sync);
    sync();
  });

  // Print buttons
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });
})();
