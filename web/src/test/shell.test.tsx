/** T-WEB-08 脚手架验收测试：视觉 token / 路由占位 / 调试态开关 / prebuild 钩子。 */
import { render, screen } from '@testing-library/react';
import { act } from 'react';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import AppShell, { simClockText } from '../components/common/AppShell';
import { useUiStore } from '../stores/uiStore';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';

describe('T-WEB-08', () => {
  it('02 §7.1 十二个 token HEX 在 tokens.css 逐字全中', () => {
    const css = readFileSync('src/styles/tokens.css', 'utf-8');
    const hexes = ['#0E1116', '#161B22', '#1F2630', '#2D333D', '#E6E9EF', '#9AA4B2',
      '#5AC3ED', '#2EFA8D', '#F75752', '#FEAE3E', '#8A93A0', '#BF86E8'];
    for (const h of hexes) expect(css).toContain(h);
  });

  it('按 d 键导航出现/隐藏"健康度"（03 §3.6）', () => {
    const router = createMemoryRouter(
      [{ path: '*', element: <AppShell /> }],
      { initialEntries: ['/map'] },
    );
    render(<RouterProvider router={router} />);
    expect(screen.queryByText('健康度')).not.toBeInTheDocument();
    act(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'd' }));
    });
    expect(screen.getByText('健康度')).toBeInTheDocument();
    act(() => {
      useUiStore.getState().toggleDebug();
    });
    expect(screen.queryByText('健康度')).not.toBeInTheDocument();
  });

  it('sim 时钟文本形态 Day X HH:mm（03 §2.1）', () => {
    expect(simClockText('2026-10-12T19:42:00+08:00', 12)).toMatch(/^Day 12 \d{2}:\d{2}$/);
    expect(simClockText(null, null)).toBe('—');
  });

  it('prebuild 钩子 = tsc --noEmit + vitest run（03 §8.2）', () => {
    const pkg = JSON.parse(readFileSync('package.json', 'utf-8'));
    expect(pkg.scripts.prebuild).toContain('tsc --noEmit');
    expect(pkg.scripts.prebuild).toContain('vitest run');
  });
});
