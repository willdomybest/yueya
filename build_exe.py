#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 willdomybest
"""
打包脚本：把 RemoteFM.py 打成单个可执行文件，并自动收集依赖的开源许可证。

用法（打包时才会用到这个文件，运行程序本身不需要它）：
    python build_exe.py

产物（dist 目录）：
    RemoteFM.exe                  可执行文件（Windows；macOS / Linux 无扩展名）
    LICENSE                       本项目 MIT 许可证
    THIRD_PARTY_NOTICES.txt       依赖清单与许可证
    licenses/<组件>-LICENSE.txt   各依赖组件的许可证全文

图标不依赖外部文件：脚本内联绘制同一枚月牙并生成 .ico（写入 build/，已忽略提交）。

分发提醒：请把可执行文件与 LICENSE、THIRD_PARTY_NOTICES.txt、licenses/ 一起打包发布。
BSD-3-Clause / Apache-2.0 / MPL-2.0 都要求分发二进制时保留版权与许可声明。
"""
import importlib.metadata as md
import math
import os
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, 'dist')
BUILD = os.path.join(ROOT, 'build')
APP_NAME = 'RemoteFM'

# 打包进可执行文件的第三方组件（用于读取版本与许可证）
PKGS = (
    'flask', 'werkzeug', 'jinja2', 'itsdangerous', 'click', 'blinker', 'markupsafe',
    'requests', 'urllib3', 'certifi', 'charset-normalizer', 'idna', 'pyftpdlib',
)
LICENSE_HINTS = ('license', 'copying', 'notice', 'authors', 'copyright')

# 与 RemoteFM.py 中内联的 favicon 完全相同的月牙几何
MOON_BLUE = (74, 108, 247)
MOON_R = 26.0
MOON_OFFSET = 10.0
MOON_ANGLE = -20.0


# ---------------- 生成图标（纯标准库，不依赖 Pillow） ----------------

def _crescent_rgba(size, ss=4):
    """按 64x64 设计稿绘制月牙，返回 RGBA 字节。ss 为超采样倍数。"""
    theta = math.radians(MOON_ANGLE)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    k = 64.0 / size
    rows = bytearray()
    for py in range(size):
        for px in range(size):
            hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) * k - 32.0
                    y = (py + (sy + 0.5) / ss) * k - 32.0
                    # 反向旋转，把像素映射回未旋转的设计坐标
                    rx = x * cos_t + y * sin_t
                    ry = -x * sin_t + y * cos_t
                    if rx * rx + ry * ry <= MOON_R * MOON_R:
                        if (rx - MOON_OFFSET) ** 2 + ry * ry > MOON_R * MOON_R:
                            hits += 1
            if hits:
                rows += bytes(MOON_BLUE) + bytes([round(255 * hits / (ss * ss))])
            else:
                rows += b'\x00\x00\x00\x00'
    return bytes(rows)


def _png(size, rgba):
    raw = b''.join(b'\x00' + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9))
            + chunk(b'IEND', b''))


def write_icon(path, sizes=(16, 20, 24, 32, 48, 64, 128, 256)):
    entries, blob, offset = [], b'', 6 + 16 * len(sizes)
    for s in sizes:
        data = _png(s, _crescent_rgba(s))
        entries.append(struct.pack('<BBBBHHII', s % 256, s % 256, 0, 0, 1, 32, len(data), offset))
        blob += data
        offset += len(data)
    with open(path, 'wb') as fh:
        fh.write(struct.pack('<HHH', 0, 1, len(sizes)) + b''.join(entries) + blob)


# ---------------- 打包与许可证收集 ----------------

def ensure_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print('[1/3] 未安装 PyInstaller，正在自动安装 ...')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyinstaller'])


def build_binary():
    print('[2/3] 正在打包，首次执行需要 1-2 分钟 ...')
    os.makedirs(BUILD, exist_ok=True)
    icon = os.path.join(BUILD, 'icon.ico')
    write_icon(icon)
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
           '--name', APP_NAME, '--icon', icon, 'RemoteFM.py']
    subprocess.check_call(cmd, cwd=ROOT)


def license_of(dist, files=()):
    """优先用元数据里的 SPDX 表达式，其次看许可证文件首行，最后退回 classifier。"""
    expr = dist.metadata.get('License-Expression')
    if expr:
        return ' '.join(expr.split())

    lic = ' '.join((dist.metadata.get('License') or '').split())
    if lic and len(lic) <= 40 and '::' not in lic:
        return lic

    for name in files:
        try:
            with open(os.path.join(DIST, 'licenses', name), encoding='utf-8', errors='ignore') as fh:
                head = ' '.join(fh.readline().split())
        except OSError:
            continue
        if head and len(head) <= 40 and any(k in head for k in ('MIT', 'BSD', 'Apache', 'MPL', 'ISC', 'GPL')):
            return head

    classes = [c.split('::')[-1].strip() for c in dist.metadata.get_all('Classifier', [])
               if c.startswith('License ::')]
    return '；'.join(classes) if classes else '见 licenses/ 目录'


def collect_licenses():
    print('[3/3] 正在收集依赖许可证 ...')
    os.makedirs(DIST, exist_ok=True)
    lic_dir = os.path.join(DIST, 'licenses')
    if os.path.isdir(lic_dir):
        shutil.rmtree(lic_dir)
    os.makedirs(lic_dir)

    rows, copied = [], []
    for name in PKGS:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            rows.append((name, '-', '未安装'))
            continue
        files = []
        for f in dist.files or []:
            path = str(f).replace('\\', '/')
            base = os.path.basename(path)
            if '.dist-info/' in path and base.lower().startswith(LICENSE_HINTS):
                if base.lower().endswith(('.py', '.pyc', '.json')):
                    continue
                target = '%s-%s' % (name, base)
                try:
                    shutil.copyfile(dist.locate_file(f), os.path.join(lic_dir, target))
                    files.append(target)
                except OSError:
                    continue
        rows.append((name, dist.version, license_of(dist, files)))
        copied += files

    shutil.copyfile(os.path.join(ROOT, 'LICENSE'), os.path.join(DIST, 'LICENSE'))
    lines = [
        'RemoteFM 第三方组件声明',
        '=' * 64,
        '',
        'RemoteFM 以 MIT 协议发布，许可证全文见同目录下的 LICENSE。',
        '可执行文件中包含下列第三方组件，版权归各自作者所有，许可证均与 MIT 兼容；',
        '各组件的许可证全文见 licenses/ 目录（未随包提供者请见组件官方仓库）。',
        '',
        '%-20s %-12s %s' % ('组件', '版本', '许可证'),
        '-' * 64,
    ]
    lines += ['%-20s %-12s %s' % r for r in rows]
    lines += ['', '许可证文件：', '-' * 64]
    lines += ['licenses/' + f for f in copied] if copied else ['（未找到许可证文件）']
    lines += ['', '分发提醒：发布二进制时请保留 LICENSE、本文件与 licenses/ 目录。', '']
    with open(os.path.join(DIST, 'THIRD_PARTY_NOTICES.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines))
    print('      已收集 %d 个许可证文件' % len(copied))


def main():
    ensure_pyinstaller()
    build_binary()
    collect_licenses()
    exe = APP_NAME + ('.exe' if os.name == 'nt' else '')
    print('\n完成：%s' % os.path.join(DIST, exe))
    print('发布时请把可执行文件与 LICENSE、THIRD_PARTY_NOTICES.txt、licenses/ 一起打包。')


if __name__ == '__main__':
    main()
