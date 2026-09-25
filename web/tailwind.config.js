/** @type {import('tailwindcss').Config} */
// 视觉 token 唯一持有方 = 02 §7.1（HEX 逐字镜像在 src/styles/tokens.css；此处只映射 CSS 变量）
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        'bg-0': 'var(--bg-0)',
        'bg-1': 'var(--bg-1)',
        'bg-2': 'var(--bg-2)',
        border: 'var(--border)',
        'text-0': 'var(--text-0)',
        'text-1': 'var(--text-1)',
        accent: 'var(--accent)',
        positive: 'var(--positive)',
        negative: 'var(--negative)',
        warn: 'var(--warn)',
        neutral: 'var(--neutral)',
        director: 'var(--director)',
      },
      borderRadius: { card: '8px' },
      spacing: { card: '12px' },
      fontSize: {
        title: '14px',
        body: '13px',
        aux: '12px',
        ts: '11px',
      },
    },
  },
  plugins: [],
};
