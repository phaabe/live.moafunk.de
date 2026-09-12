import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import router from './router';
import { staticHostRedirectTarget } from './staticHostRedirect';
import '@shared/styles/tokens.css';
import './styles/admin.css';
import 'v-calendar/style.css';

// This bundle is also published to GitHub Pages, where there is no API behind
// it (login POSTs would answer 405). Leave before mounting anything.
const redirect = staticHostRedirectTarget(window.location);
if (redirect) {
  window.location.replace(redirect);
} else {
  const app = createApp(App);

  app.use(createPinia());
  app.use(router);

  app.mount('#app');
}
