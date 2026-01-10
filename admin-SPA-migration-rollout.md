# Admin SPA Migration Rollout

## Phase 1: Foundation (Week 1) ✅
- [x] Add Vue 3, Vue Router, Pinia to frontend
- [x] Configure Vite multi-page build with admin entry
- [x] Create shared CSS tokens and base components
- [x] Add `/api/auth/login`, `/api/auth/me`, `/api/auth/logout` endpoints
- [x] Build LoginPage.vue with auth store
- **Checkpoint:** Can login via SPA, session cookie works ✅

## Phase 2: Artists & Shows (Week 2) ✅
- [x] Add `/api/artists` CRUD endpoints
- [x] Build ArtistsPage.vue (list with filters/sorting)
- [x] Build ArtistDetailPage.vue (view/edit/delete)
- [x] Add `/api/shows` CRUD endpoints
- [x] Build ShowsPage.vue and ShowDetailPage.vue
- **Checkpoint:** Full artist/show management in SPA ✅

## Phase 3: Users & Polish (Week 3)
- [ ] Add `/api/users` CRUD endpoints
- [ ] Build UsersPage.vue and ChangePasswordPage.vue
- [ ] Add error handling, loading states, flash messages
- [ ] Test all CRUD operations end-to-end
- **Checkpoint:** All management pages complete

## Phase 4: Stream Control (Week 4)
- [ ] Extract useStreamSocket() composable (WebSocket + reconnect)
- [ ] Extract useAudioCapture() composable (device selection, MediaRecorder)
- [ ] Extract useAudioMeter() composable (Web Audio API visualization)
- [ ] Build StreamPage.vue with all streaming functionality
- [ ] Test streaming stability thoroughly
- **Checkpoint:** Streaming works identically to Tera version

## Phase 5: Cleanup (Week 5)
- [ ] Remove Tera templates from backend/templates/
- [ ] Remove `tera` dependency from Cargo.toml
- [ ] Remove template-rendering routes from handlers
- [ ] Update deployment scripts
- [ ] Document new architecture in README
- **Done:** Full SPA admin panel live
