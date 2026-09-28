// Small progressive enhancements. Every page works without this file; it only adds:
// the phone menu toggle, opening a chat at its latest turn, Ctrl/Cmd+Enter to send, and on the
// Workflows page a preview of each chosen input image plus the list's search box.
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

  // Workflows: the listeners sit on document, so they keep working after htmx swaps the list and panel.
  function sizeLabel(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  function updateProgress(form) {
    var inputs = form.querySelectorAll(".wf-well input[type=file]");
    var note = form.querySelector(".wf-progress");
    if (!inputs.length || !note) return;
    var chosen = 0;
    inputs.forEach(function (input) { if (input.files && input.files.length) chosen += 1; });
    note.textContent = chosen === inputs.length
      ? (inputs.length === 1 ? "Image chosen." : "All " + inputs.length + " images chosen.")
      : chosen + " of " + inputs.length + " image" + (inputs.length === 1 ? "" : "s") + " chosen.";
  }

  function showFile(input) {
    var slot = input.closest(".wf-slot");
    var well = slot.querySelector(".wf-well");
    var img = well.querySelector("img");
    var hint = well.querySelector(".wf-well-hint");
    var info = slot.querySelector(".wf-file");
    var clear = slot.querySelector(".wf-clear");
    if (img.src) URL.revokeObjectURL(img.src);
    var file = input.files && input.files[0];
    if (!info.dataset.node) info.dataset.node = info.textContent;
    if (file) {
      img.src = URL.createObjectURL(file);
      img.hidden = false;
      hint.hidden = true;
      clear.hidden = false;
      well.classList.add("filled");
      info.textContent = file.name + " \u00b7 " + sizeLabel(file.size);
    } else {
      img.removeAttribute("src");
      img.hidden = true;
      hint.hidden = false;
      clear.hidden = true;
      well.classList.remove("filled");
      info.textContent = info.dataset.node;
    }
    updateProgress(input.form);
  }

  document.addEventListener("change", function (event) {
    if (event.target.matches(".wf-well input[type=file]")) showFile(event.target);
  });

  document.addEventListener("click", function (event) {
    var clear = event.target.closest(".wf-clear");
    if (!clear) return;
    var input = clear.closest(".wf-slot").querySelector("input[type=file]");
    input.value = "";
    showFile(input);
  });

  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var well = event.target.closest && event.target.closest(".wf-well");
      if (well) well.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (event) {
      var well = event.target.closest && event.target.closest(".wf-well");
      if (well) well.classList.remove("dragging");
    });
  });

  document.addEventListener("input", function (event) {
    if (!event.target.matches(".wf-search")) return;
    var query = event.target.value.trim().toLowerCase();
    document.querySelectorAll(".wf-list .wf-row").forEach(function (row) {
      var name = row.querySelector(".wf-row-name").textContent.toLowerCase();
      row.hidden = query !== "" && name.indexOf(query) === -1;
    });
  });
})();
