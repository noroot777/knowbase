/** 扩展弹窗那两条探针共用的假 DOM(`browser_capture_gate.mjs`、`grab_from_page_gate.mjs`)。
 *
 *  **共用是因为被咬过。** 两处原先各写一份:「指着别的地址」那一块改成按服务端交回
 *  几条就摆几条之后,`setBusy` 要从那张列表上现读那些按钮,给一处的桩补上
 *  `querySelectorAll` —— 另一处当场 `TypeError`,而那条探针根本不碰这一块。
 *
 *  **它真的记住 children**:那些行是现画出来的、逐行自带一颗按钮,只认得 id 的桩问
 *  不出「点第二行那颗,交的是第二条的 key 吗」。
 */
export function node(tag = '') {
  return {
    tag, value: '', textContent: '', className: '', type: '', dataset: {},
    checked: false, disabled: false, hidden: false, open: false, children: [],
    classList: {toggle() {}, add() {}, remove() {}},
    append(...kids) { this.children.push(...kids); },
    replaceChildren(...kids) { this.children = kids; },
    querySelectorAll(selector) {
      return this.children.flatMap(kid =>
        (kid.tag === selector ? [kid] : []).concat(kid.querySelectorAll(selector)));
    },
    addEventListener(name, callback) { this[name] = callback; }
  };
}

export function popupDom() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  };
  return {element, document: {getElementById: element, createElement: node}};
}
