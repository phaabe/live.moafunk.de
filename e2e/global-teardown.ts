import { RUN, staging } from './lib/staging';

export default function globalTeardown(): void {
  console.log(staging(`e2e-unhold ${RUN}`));
}
