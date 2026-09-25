/** /location/:id 地点详情页（05 T-WEB-15；03 §1.2 行）。主体实现见 AgentPage.tsx 的 LocationPageBody。 */
import { useParams } from 'react-router-dom';
import { LocationPageBody } from './AgentPage';

export default function LocationPage() {
  const { id = '' } = useParams();
  return <LocationPageBody id={decodeURIComponent(id)} />;
}
