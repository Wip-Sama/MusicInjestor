import React, { useEffect } from 'react';
import {
  init,
  miniApp,
  viewport,
  swipeBehavior,
  themeParams,
} from '@telegram-apps/sdk-react';
import { getRawInitData } from '../lib/auth';

interface TelegramBootstrapProps {
  children: React.ReactNode;
}

export const TelegramBootstrap: React.FC<TelegramBootstrapProps> = ({ children }) => {
  useEffect(() => {
    // 1. Prime sessionStorage with initData before any routing happens
    getRawInitData();

    // 2. Initialize Telegram Apps SDK
    try {
      init();

      if (miniApp.mount.isAvailable()) {
        miniApp.mount();
        // Section 1.2: Immediate miniApp.ready() call to dismiss loading spinner (<2.5s)
        miniApp.ready();
      }

      if (viewport.mount.isAvailable()) {
        viewport.mount().then(() => {
          if (viewport.expand.isAvailable()) {
            viewport.expand();
          }
        });
      }

      if (swipeBehavior.mount.isAvailable()) {
        swipeBehavior.mount();
        // Section 5.3: Prevent accidental drawer closures when scrolling catalog
        swipeBehavior.disableVertical();
      }

      if (themeParams.mount.isAvailable()) {
        themeParams.mount();
      }
    } catch (err) {
      console.warn('Telegram SDK initialization skipped or running outside Telegram container:', err);
    }
  }, []);

  return <>{children}</>;
};

