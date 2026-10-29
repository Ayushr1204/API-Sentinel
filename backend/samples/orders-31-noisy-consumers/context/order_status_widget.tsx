// Sample consumer (fixture): customer web widget showing an order's progress.
export function OrderStatus({ order }: { order: Order }) {
  const link = order.trackingUrl ?? null;
  return (
    <div>
      <p>Status: {order.state}</p>
      {link && <a href={link}>Track parcel</a>}
    </div>
  );
}
type Order = { id: string; state: string; total: number; trackingUrl?: string | null };
