// Datasette (/sql/): a way back to the app, first in the breadcrumbs.
document.addEventListener("DOMContentLoaded", () => {
  const crumbs = document.querySelector("header .crumbs") || document.querySelector("header");
  if (!crumbs) return;
  const back = Object.assign(document.createElement("a"), { href: "/#data", className: "home-franziska", textContent: "‹ Franziska" });
  crumbs.prepend(back);
});
