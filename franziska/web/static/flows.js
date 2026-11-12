// A workflow's run form, drawn from its fields (GET /api/workflows): the classic Workflows view and the world's card
// both use it, each with its own `h` and styles. A new workflow or param shows up here with no change.
//   field: { name, label, help, kind: integer | number | string | boolean | choice | date, options, default, min, max, optional }

// The text a run is logged under (the server writes the same): '/mail_bills days=30 about=condominio'.
export const runLabel = (wf, params) =>
  [`/${wf.name}`, ...Object.entries(params).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${v}`)].join(" ");

export function flowForm(wf, h, { onRun, disabled = false, compact = false } = {}) {
  const read = [];  // one () => [name, value] per field
  const field = (f) => {
    const id = `wf-${wf.name}-${f.name}`;
    let input;
    if (f.kind === "choice" && f.options.length <= 4) {
      let value = f.default ?? f.options[0];
      const seg = h("div", { class: "seg", role: "radiogroup", "aria-labelledby": `${id}-label` });
      const draw = () => seg.replaceChildren(...f.options.map((o) => h("button", {
        type: "button", role: "radio", "aria-checked": String(o === value), onclick: () => { value = o; draw(); },
      }, String(o))));
      draw();
      read.push(() => [f.name, value]);
      input = seg;
    } else if (f.kind === "choice") {
      input = h("select", { id }, f.options.map((o) => h("option", { value: o, selected: o === f.default }, String(o))));
      read.push(() => [f.name, input.value]);
    } else if (f.kind === "boolean") {
      input = h("input", { id, type: "checkbox", checked: !!f.default });
      read.push(() => [f.name, input.checked]);
    } else {
      const number = f.kind === "integer" || f.kind === "number";
      input = h("input", {
        id, type: number ? "number" : f.kind === "date" ? "date" : "text",
        value: f.default ?? "", min: f.min, max: f.max, step: f.kind === "integer" ? 1 : number ? "any" : null,
        placeholder: f.optional ? "any" : null, inputmode: f.kind === "integer" ? "numeric" : null,
        required: !f.optional && f.default == null,
      });
      read.push(() => {
        const v = input.value.trim();
        return [f.name, v === "" ? (f.optional ? null : f.default) : number ? Number(v) : v];
      });
    }
    return h("div", { class: `field${f.kind === "boolean" ? " inline" : ""}` },
      h("label", { for: id, id: `${id}-label`, title: compact ? f.help : null }, f.label), input,
      f.help && !compact ? h("small", {}, f.help) : "");
  };
  const run = h("button", { type: "submit", class: "run", disabled }, h("span", { class: "play" }), "Run");
  const form = h("form", { class: "wf-form", onsubmit: (e) => {
    e.preventDefault();
    if (run.disabled) return;
    onRun?.(Object.fromEntries(read.map((r) => r())), form);
  } },
    wf.fields.length ? h("div", { class: "fields" }, wf.fields.map(field)) : h("div", { class: "no-fields" }, "No settings: it runs as is."),
    run);
  form.running = (on) => { run.disabled = on; run.classList.toggle("going", on); };
  return form;
}
