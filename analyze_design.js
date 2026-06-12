const fs = require('fs');
let raw = fs.readFileSync('E:/geo/design_analysis.json', 'utf8');
let data;
try { data = JSON.parse(raw); } catch(e) { console.log('JSON parse error:', e.message); process.exit(1); }

function findNode(node, targetId) {
  if (!node || typeof node !== 'object') return null;
  if (node.id === targetId) return node;
  if (node.children && Array.isArray(node.children)) {
    for (const child of node.children) {
      const found = findNode(child, targetId);
      if (found) return found;
    }
  }
  return null;
}

function describeNode(node, depth) {
  if (!node || typeof node !== 'object') return '';
  if (depth > 6) return '';
  const indent = '  '.repeat(depth);
  let result = '';
  
  const type = node.type || '?';
  const name = node.name || '';
  const id = node.id || '';
  const w = node.width !== undefined ? node.width : 'auto';
  const h = node.height !== undefined ? node.height : 'auto';
  
  let info = indent + type + ' "' + name + '"';
  if (w !== 'auto' || h !== 'auto') info += ' [' + w + ' x ' + h + ']';
  
  if (node.layout) info += ' layout:' + node.layout;
  if (node.padding) info += ' pad:' + JSON.stringify(node.padding);
  if (node.gap) info += ' gap:' + node.gap;
  if (node.fill && typeof node.fill === 'string') info += ' fill:' + node.fill;
  if (node.justifyContent) info += ' jc:' + node.justifyContent;
  if (node.alignItems) info += ' ai:' + node.alignItems;
  if (node.cornerRadius) info += ' radius:' + node.cornerRadius;
  if (node.stroke) info += ' stroke:' + node.stroke;
  if (node.strokeWidth) info += ' sw:' + JSON.stringify(node.strokeWidth);
  if (node.effect) info += ' [shadow]';
  if (node.reusable) info += ' [REUSABLE]';
  if (node.layoutPosition) info += ' pos:' + node.layoutPosition;
  if (node.lineHeight) info += ' lh:' + node.lineHeight;
  if (node.textGrowth) info += ' grow:' + node.textGrowth;
  if (node.textAlign) info += ' align:' + node.textAlign;
  if (node.letterSpacing) info += ' ls:' + node.letterSpacing;
  
  if (node.content && typeof node.content === 'string' && node.type === 'text') {
    info += ' text:"' + node.content + '"';
    if (node.fontFamily) info += ' font:' + node.fontFamily;
    if (node.fontSize) info += ' size:' + node.fontSize;
    if (node.fontWeight) info += ' weight:' + node.fontWeight;
  }
  
  if (node.type === 'icon') {
    info += ' icon:' + node.icon + ' lib:' + (node.library || '');
  }
  
  result += info + '\n';
  
  if (node.children && Array.isArray(node.children)) {
    for (const child of node.children) {
      result += describeNode(child, depth + 1);
    }
  }
  
  return result;
}

const targetIds = ['Z4Bc9D', 'k8hUI3', 'f8fr6', 'Gp70U', 'a4Y2P', 'FjZvn', 'Fm9QX', 'YamKh', 'VRpre', 'P377K'];

for (const tid of targetIds) {
  const node = findNode({children: data}, tid);
  if (node) {
    console.log('='.repeat(80));
    console.log('FRAME: ' + node.name + ' (id=' + tid + ')');
    console.log('='.repeat(80));
    console.log(describeNode(node, 0));
  } else {
    console.log('='.repeat(80));
    console.log('FRAME id=' + tid + ' NOT FOUND at top level');
    console.log('='.repeat(80));
  }
}
