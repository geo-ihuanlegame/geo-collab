const fs = require('fs');
let s = fs.readFileSync('E:/geo/design_analysis_clean.json', 'utf8');
let data = JSON.parse(s);

function findNode(nodes, id) {
  for (const n of nodes) {
    if (n.id === id) return n;
    if (n.children) { const f = findNode(n.children, id); if (f) return f; }
  }
  return null;
}

function desc(n, d) {
  if (!n || d > 5) return '';
  let r = '';
  let info = '  '.repeat(d) + n.type;
  if (n.name) info += ' "' + n.name + '"';
  if (n.width !== undefined) info += ' w:' + n.width;
  if (n.height !== undefined) info += ' h:' + n.height;
  if (n.layout) info += ' layout:' + n.layout;
  if (n.padding) info += ' pad:' + JSON.stringify(n.padding);
  if (n.gap) info += ' gap:' + n.gap;
  if (n.fill && typeof n.fill === 'string') info += ' fill:' + n.fill;
  if (n.justifyContent) info += ' jc:' + n.justifyContent;
  if (n.alignItems) info += ' ai:' + n.alignItems;
  if (n.cornerRadius) info += ' radius:' + n.cornerRadius;
  if (n.stroke) info += ' stroke:' + n.stroke;
  if (n.strokeWidth) info += ' sw:' + JSON.stringify(n.strokeWidth);
  if (n.reusable) info += ' [REUSABLE]';
  if (n.layoutPosition) info += ' pos:' + n.layoutPosition;
  if (n.effect) info += ' [shadow]';
  if (n.content && typeof n.content === 'string' && n.type === 'text') {
    info += ' text:"' + n.content + '"';
    if (n.fontFamily) info += ' font:' + n.fontFamily;
    if (n.fontSize) info += ' size:' + n.fontSize;
    if (n.fontWeight) info += ' weight:' + n.fontWeight;
    if (n.textAlign) info += ' align:' + n.textAlign;
    if (n.letterSpacing) info += ' ls:' + n.letterSpacing;
    if (n.lineHeight) info += ' lh:' + n.lineHeight;
  }
  if (n.type === 'icon') info += ' icon:"' + n.icon + '" lib:' + n.library;
  r += info + '\n';
  if (n.children) for (const c of n.children) r += desc(c, d + 1);
  return r;
}

const ids = ['Z4Bc9D', 'k8hUI3', 'f8fr6', 'Gp70U', 'a4Y2P', 'FjZvn', 'Fm9QX', 'YamKh', 'VRpre', 'P377K'];
for (const id of ids) {
  const n = findNode(data, id);
  if (n) {
    console.log('='.repeat(60));
    console.log('ID: ' + id + '   NAME: ' + n.name);
    console.log('='.repeat(60));
    console.log(desc(n, 0));
  } else {
    console.log(id + ': NOT FOUND');
  }
}
