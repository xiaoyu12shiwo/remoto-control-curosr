'use strict';

const fs = require('fs');
const path = require('path');

const extRoot = path.join(__dirname, '..');
const srcRoot = path.join(extRoot, '..');
const destRoot = path.join(extRoot, 'server');

const COPY_FILES = [
  'remote_control_server.py',
  'remote_control.py',
  'remote_control_send.py',
  'input_box.png',
  'send_icon.png',
  'mic_icon.png',
];

function copyFile(name) {
  const from = path.join(srcRoot, name);
  const to = path.join(destRoot, name);
  if (!fs.existsSync(from)) {
    console.warn(`跳过（不存在）: ${name}`);
    return;
  }
  fs.copyFileSync(from, to);
  console.log(`已复制: ${name}`);
}

if (!fs.existsSync(destRoot)) {
  fs.mkdirSync(destRoot, { recursive: true });
}

for (const name of COPY_FILES) {
  copyFile(name);
}

console.log(`\n打包资源已写入: ${destRoot}`);
console.log('接下来可执行: npm run package');
