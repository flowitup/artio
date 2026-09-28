// Small progressive enhancements. Every page works without this file; it only adds:
// the phone menu toggle, opening a chat at its latest turn, Ctrl/Cmd+Enter to send, and re-pricing
// the composer's cost estimate when its size or resolution changes.
(function () {
  "use strict";

  // Phone layout: the sidebar collapses to a top bar and this button shows or hides the rest.
  var toggle = document.querySelector(".menu-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var sidebar = toggle.closest(".sidebar");
      var open = sidebar.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // A chat opens at its newest turn, unless the URL already points somewhere (#turn-12, #composer).
  var log = document.querySelector(".chat-log");
  if (log && !location.hash) {
    var turns = log.querySelectorAll(".turn");
    if (turns.length) {
      turns[turns.length - 1].scrollIntoView({ block: "start" });
    }
  }

  // Ctrl+Enter (Cmd+Enter on a Mac) in the message box sends it.
  var composer = document.getElementById("composer");
  if (composer) {
    var text = composer.querySelector("textarea");
    if (text) {
      text.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          composer.requestSubmit();
        }
      });
    }

    // The server prices the size the composer opened with; picking another shape or resolution
    // scales that estimate by pixel count, the same way the server does.
    var estimate = composer.querySelector(".estimate[data-per-megapixel]");
    if (estimate) {
      var perMegapixel = parseFloat(estimate.dataset.perMegapixel);
      var sizes = JSON.parse(estimate.dataset.sizes);
      composer.addEventListener("change", function (event) {
        if (event.target.name !== "preset" && event.target.name !== "tier") return;
        var shape = composer.querySelector('input[name="preset"]:checked');
        var tier = composer.querySelector('input[name="tier"]:checked');
        var size = shape && sizes[shape.value + "|" + (tier ? tier.value : "")];
        if (!size) return;
        var cost = perMegapixel * size[0] * size[1] / 1e6;
        estimate.textContent = cost < 0.01 ? "under $0.01 per image" : "≈ $" + cost.toFixed(2) + " per image";
        estimate.title = size[0] + "×" + size[1];
      });
    }
  }
})();
