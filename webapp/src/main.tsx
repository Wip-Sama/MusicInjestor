/// <reference types="vite/client" />
import React from 'react';
import ReactDOM from 'react-dom/client';
import '@telegram-apps/telegram-ui/dist/styles.css';
import { TelegramBootstrap } from './components/TelegramBootstrap';
import { App } from './App';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <TelegramBootstrap>
      <App />
    </TelegramBootstrap>
  </React.StrictMode>,
);

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .then((reg) => {
        console.debug('Service Worker registered successfully:', reg.scope);
      })
      .catch((err) => {
        console.warn('Service Worker registration failed:', err);
      });
  });
}

