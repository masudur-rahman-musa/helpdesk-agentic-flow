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
 * Events in the server's order, never re-sorted. Times show the ISO prefix: a
 * locale format can show a different day across time zones.
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
            {/* Raw type: the list is open, and it is what goes in the filter. */}
            <TableCell>{event.event_type}</TableCell>
            <TableCell>{event.actor_kind}</TableCell>
            <TableCell>{event.actor_ref ?? "no actor"}</TableCell>
            <TableCell>{event.subject_type}</TableCell>
            <TableCell>{event.subject_id ?? "no subject id"}</TableCell>
            <TableCell title={event.hash}>{event.hash.slice(0, 12)}</TableCell>
            <TableCell>
              {/* Payload shape differs per event type, so it shows whole. */}
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
