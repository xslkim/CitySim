/** T-LTV-06 飘屏占位组件（DOM 绑定；不接数据源、不做交互，04 §6.4 互动指向"下一段"排期口径）。
 *
 * 两个占位条（投票结果位 / 礼物致谢位）样式按原型；默认只显示静态提示文案
 * （"互动只影响下一段"语义）；URL 参数 ?float=0 整组隐藏。
 * 数据接口留白（update 方法 stub）：礼物/投票事件接入属 defer 项（06 §2）。
 * A7 红线：严禁平台 UI 元素（check_stream_layout.py grep 把关）。
 */

export class FloatView {
  /** @param mount #float-mount；@param opts.visible 整组显隐（?float=0 隐藏） */
  constructor(mount, { visible = true } = {}) {
    this.mount = mount;
    this.mount.hidden = !visible;
    if (visible) this.render();
  }

  render() {
    this.mount.textContent = '';
    const vote = document.createElement('div');
    vote.className = 'float-vote';
    vote.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><rect x="4" y="4" width="16" height="16" rx="3"/><polyline points="8.5 12.5 11 15 15.5 9.5"/></svg>';
    const voteText = document.createElement('span');
    voteText.textContent = '投票结果位';
    const voteNext = document.createElement('span');
    voteNext.className = 'next';
    voteNext.textContent = '互动只影响下一段';
    vote.append(voteText, voteNext);

    const gift = document.createElement('div');
    gift.className = 'gift-float';
    gift.innerHTML = '<div class="gf-row1"><svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round"><rect x="4" y="10" width="16" height="10" rx="1.5"/><line x1="12" y1="10" x2="12" y2="20"/><rect x="3" y="6.5" width="18" height="3.5" rx="1"/><path d="M12 6.5C10 6.5 8.5 5 9 3.5c.4-1.2 2-1.5 3 0 1-1.5 2.6-1.2 3 0 .5 1.5-1 3-3 3z"/></svg><b>礼物致谢位</b></div>';
    const row2 = document.createElement('div');
    row2.className = 'gf-row2';
    row2.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><polyline points="4 12 19 12"/><polyline points="13 6 19 12 13 18"/></svg>';
    const row2Text = document.createElement('span');
    row2Text.textContent = '互动只影响下一段排期';
    row2.appendChild(row2Text);
    gift.appendChild(row2);

    this.mount.append(vote, gift);
  }

  /** 数据接口留白（06 §2 defer：gift/vote 事件接入属直播互动阶段）。 */
  update(_props = {}) { /* stub：阶段 1 不接数据源 */ }
}
