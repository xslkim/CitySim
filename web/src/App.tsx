/**
 * App 入口：移动端 UA → "桌面优先"提示页（03 §9.2 兼容性行）；桌面 → Router。
 */
import Router from './router';

function isMobileUA(): boolean {
  return /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent);
}

export default function App() {
  if (isMobileUA()) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg-0 p-8">
        <div className="card max-w-md text-center">
          <h1 className="text-title text-text-0">WorldSim 观察端桌面优先</h1>
          <p className="mt-2 text-body text-text-1">
            观察端为桌面端设计（≥1280px，03 §2.1）；请用桌面浏览器访问。
          </p>
        </div>
      </div>
    );
  }
  return <Router />;
}
