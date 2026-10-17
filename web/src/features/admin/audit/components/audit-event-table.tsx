import type { AuditEventRow } from "@/features/admin/audit/schema";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/**
 * The page of events, newest first, in the order the tool answered.
 *
 * Nothing is re-sorted: 13b returns newest first and an ordering imposed here
 * would be a second opinion about the one thing the log exists to show. The
 * timestamp renders as its 19-character ISO prefix, never
 * `toLocaleDateString` (which answers a different day either side of a date
 * line) and never the raw value, whose six digits of microseconds cost a column
 * an auditor scans.
 */
export function AuditEventTable({ events }: { events: AuditEventRow[] }) {
  return (
    <Table aria-label="Audit events">
      <TableHeader>
        <TableRow>
          <TableHead scope="col">Seq</TableHead>
          <TableHead scope="col">Occurred</TableHead>
          <TableHead scope="col">Event</TableHead>
          <TableHead scope="col">Actor kind</TableHead>
          <TableHead scope="col">Actor</TableHead>
          <TableHead scope="col">Subject</TableHead>
          <TableHead scope="col">Subject id</TableHead>
          <TableHead scope="col">Hash</TableHead>
          <TableHead scope="col">Payload</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event) => (
          <TableRow key={event.seq} className="align-top">
            <TableCell>{event.seq}</TableCell>
            <TableCell>{event.occurred_at.slice(0, 19)}</TableCell>
            {/* The raw `event_type` (decision B): the vocabulary is open, so a
                hand-kept map would be stale the day a row adds a type, and the
                literal is what an auditor pastes back into the filter. */}
            <TableCell>{event.event_type}</TableCell>
            <TableCell>{event.actor_kind}</TableCell>
            <TableCell>{event.actor_ref ?? "no actor"}</TableCell>
            <TableCell>{event.subject_type}</TableCell>
            <TableCell>{event.subject_id ?? "no subject id"}</TableCell>
            <TableCell title={event.hash}>{event.hash.slice(0, 12)}</TableCell>
            <TableCell>
              {/* `payload` is an arbitrary object by design (13b decision E),
                  so it is stringified whole rather than read key by key: a row
                  reading named keys renders blanks for every event type but
                  the one it was written for. Collapsed, because a dozen lines
                  of JSON between every two rows is not a table anyone scans. */}
              <details aria-label={`Payload for event ${event.seq}`}>
                <summary>Payload</summary>
                <pre className="whitespace-pre-wrap text-xs">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              </details>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
