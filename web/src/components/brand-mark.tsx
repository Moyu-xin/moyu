import Image from "next/image";

export function BrandMark({ className = "size-10" }: { className?: string }) {
  return <Image src="/moyu-mark.svg" alt="摸鱼机" width={64} height={64} className={`shrink-0 ${className}`} />;
}
