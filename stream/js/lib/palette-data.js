// 生成物勿手改（由 server/scripts/check_delta_e.py --export 自 ui/assets/palette/worldsim.gpl 派生，06 T-ART-01/02）
/** 签名色池（02 §4.3）：编号字符串 → {name, hex} */
export const SIGNATURE_COLORS = {
  '1': { name: '朱红', hex: '#BE2A42' },
  '2': { name: '钴蓝', hex: '#5583C9' },
  '3': { name: '翠绿', hex: '#209C68' },
  '4': { name: '琥珀', hex: '#BF700F' },
  '5': { name: '青碧', hex: '#209894' },
  '6': { name: '暗红', hex: '#61191E' },
  '7': { name: '堇紫', hex: '#4A2999' },
  '8': { name: '天青', hex: '#1A5E80' },
  '9': { name: '杏黄', hex: '#FADB40' },
  '10': { name: '鼠尾草绿', hex: '#A4C394' },
  '11': { name: '紫藤', hex: '#A432A1' },
  '12': { name: '蔷薇', hex: '#EB6999' },
  '13': { name: '咖啡', hex: '#7D4F30' },
  '14': { name: '雾蓝', hex: '#C3CDDA' },
  '15': { name: '芥末', hex: '#A8B82E' },
  '16': { name: '岩灰', hex: '#6A6C6F' },
};

/** 语义色（02 §7.1，只做边/条/图标） */
export const SEMANTIC_COLORS = {
  positive: '#2EFA8D',
  negative: '#F75752',
  warn: '#FEAE3E',
  neutral: '#8A93A0',
  director: '#BF86E8',
  accent: '#5AC3ED',
};

/** 底色/文字/描边 token（02 §7.1） */
export const TOKENS = {
  'bg-0': '#0E1116',
  'bg-1': '#161B22',
  'bg-2': '#1F2630',
  'border': '#2D333D',
  'text-0': '#E6E9EF',
  'text-1': '#9AA4B2',
};
