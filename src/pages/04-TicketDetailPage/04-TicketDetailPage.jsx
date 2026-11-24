import { analysisLabel, detailTime, useTicketData } from '../03-TicketListPage/ticketData.js';
import './04-TicketDetailPage.css';

const messageLabels = { inquiry: '고객 문의', reply: '상담사 답변', memo: '내부 메모' };
const channelLabels = { discord: 'Discord', gmail: 'Gmail' };

export default function TicketDetailPage({ id, mine, onExpired }) {
  const { data, error, loading, retry } = useTicketData('/api/tickets/' + encodeURIComponent(id), onExpired);
  const ticket = data?.ticket;
  const detailHref = uuid => '#04-01?ticket=' + encodeURIComponent(uuid) + (mine ? '&tab=assigned' : '');
  return <section className="ticket-detail" aria-labelledby="ticket-detail-title">
    <a className="desk-button ticket-detail-back" href={mine ? '#03-02' : '#03-01'}>
      <span aria-hidden="true">‹</span> 티켓 목록
    </a>
    <h1 id="ticket-detail-title">티켓 상세</h1>
    {loading && <p role="status">티켓을 불러오는 중입니다.</p>}
    {error && <p role="alert">{error} <button className="desk-button" onClick={retry}>다시 시도</button></p>}
    {ticket && <>
      <section className="ticket-detail-summary" aria-labelledby="selected-title">
        <p className="ticket-detail-number">#{ticket.ticket_number} · {channelLabels[ticket.channel] || ticket.channel}</p>
        <h2 id="selected-title">{ticket.title}</h2>
        <dl>{[
          ['고객 ID', ticket.customer_id],
          ['접수 시각', detailTime(ticket.created_at)],
          ['처리 상태', ticket.status], ['담당 부서', ticket.team_name || '미배정'],
          ['담당자', ticket.assignee_name || '미지정'],
          ['감성', analysisLabel(ticket, 'sentiment')], ['카테고리', analysisLabel(ticket, 'category')],
        ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      </section>
      <section className="ticket-conversation" aria-labelledby="conversation-title">
        <h2 id="conversation-title">고객 대화</h2>
        <ol>{data.messages.map((message, i) => <li key={message.message_uuid || i} data-sender={message.sender_type}>
          <header>
            <strong>{messageLabels[message.message_type] || (message.sender_type === 'customer' ? '고객' : '상담사')}</strong>
            {message.ticket_uuid === ticket.ticket_uuid ? <span>#{message.ticket_number} · 선택한 티켓</span>
              : <a href={detailHref(message.ticket_uuid)}>#{message.ticket_number}</a>}
            <time dateTime={message.created_at}>{detailTime(message.created_at)}</time>
          </header>
          <p>{message.body}</p>
        </li>)}</ol>
        {!data.messages.length && <p>등록된 메시지가 없습니다.</p>}
      </section>
      {data.duplicate_candidates?.length > 0 && <section className="ticket-conversation" aria-labelledby="candidates-title">
        <h2 id="candidates-title">다른 채널의 중복 문의 후보</h2>
        <ul>{data.duplicate_candidates.map(candidate => <li key={candidate.ticket_uuid}>
          <a className="ticket-detail-link" href={detailHref(candidate.ticket_uuid)}>
            #{candidate.ticket_number} · {channelLabels[candidate.channel] || candidate.channel} · {candidate.title}
          </a>
        </li>)}</ul>
      </section>}
    </>}
  </section>;
}
