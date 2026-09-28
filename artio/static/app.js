// Small progressive enhancements. Every page works without this file; it only adds:
// the phone menu toggle, opening a chat at its latest turn, and Ctrl/Cmd+Enter to send.
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
  }
})();
