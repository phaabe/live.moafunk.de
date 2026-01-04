# Moafunk Radio

Live streaming web radio from Moabit, Berlin.

## Features

- Live audio streaming (HLS for iOS, FLV for desktop)
- SoundCloud archive integration
- Responsive design
- Privacy-focused analytics (Plausible)

## Tech Stack

- **Frontend:** Vanilla TypeScript + Vite (in `frontend/`)
- **Archive generator:** Python 3.13 + uv (in `frontend/`, SoundCloud API integration)
- **Backend:** Rust (in `backend/`)
- **Deployment:** GitHub Actions + GitHub Pages

## Development Setup

### Prerequisites

- Node.js 18+ and npm
- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Installation

1. Clone the repository:
```sh
git clone https://github.com/yourusername/live.moafunk.de.git
cd live.moafunk.de
```

2. Switch into the frontend directory:
```sh
cd frontend
```

3. Install Node.js dependencies:
```sh
npm install
```

4. Copy environment variables template:
```sh
cp .env.example .env
```

4. Edit `.env` and configure your environment variables (see Environment Variables section below)

### Running Locally

1. Generate tracks data from SoundCloud:
```sh
cd frontend
uv run scripts/generate_relisten.py \
  --client-id "$SOUNDCLOUD_CLIENT_ID" \
  --client-secret "$SOUNDCLOUD_CLIENT_SECRET"
```

2. Start development server:
```sh
cd frontend
npm run dev
```

3. Open http://localhost:3000 in your browser

### Building for Production

**Important:** You must generate the SoundCloud tracks data before building:

```sh
# First, generate tracks data
cd frontend
uv run scripts/generate_relisten.py \
  --client-id "$SOUNDCLOUD_CLIENT_ID" \
  --client-secret "$SOUNDCLOUD_CLIENT_SECRET"

# Then build
npm run build
```

The output will be in `frontend/dist/`.

**Note:** In production (CI/CD), this is automated by the deployment workflow.

## Environment Variables

### Required for Development

Create a `.env` file with the following variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `VITE_STREAM_HLS_URL` | HLS stream URL (for iOS devices) | `https://stream.moafunk.de/live/stream-io/index.m3u8` |
| `VITE_STREAM_FLV_URL` | FLV stream URL (for desktop) | `https://stream.moafunk.de/live/stream-io.flv` |
| `VITE_ANALYTICS_DOMAIN` | Plausible analytics domain | `live.moafunk.de` |
| `VITE_ANALYTICS_SCRIPT_URL` | Plausible script URL | `https://plausible.moafunk.de/js/plausible.js` |

### Required for SoundCloud API (CI/CD)

These must be set as GitHub Secrets:

| Secret | Description | How to Get |
|--------|-------------|------------|
| `SOUNDCLOUD_CLIENT_ID` | SoundCloud OAuth Client ID | [SoundCloud Apps](https://soundcloud.com/you/apps) |
| `SOUNDCLOUD_CLIENT_SECRET` | SoundCloud OAuth Client Secret | [SoundCloud Apps](https://soundcloud.com/you/apps) |

## Project Structure

```
.
├── backend/                # Rust backend (independent)
└── frontend/
  ├── src/
  │   ├── index.html          # Main live stream page
  │   ├── main.ts             # Entry point for TypeScript
  │   ├── main.css            # Global styles
  │   ├── config.ts           # Environment configuration
  │   ├── player.ts           # Audio player logic
  │   ├── streamDetector.ts   # Platform detection & stream status
  │   └── pages/
  │       ├── re-listen.html  # Generated archive page (gitignored)
  │       └── tech-rider.html # Equipment info page
  ├── public/
  │   ├── sc-img.html         # SoundCloud artwork editor (standalone)
  │   ├── moafunk.png         # Logo
  │   ├── icons/              # Favicons & PWA icons
  │   └── CNAME               # GitHub Pages domain
  ├── scripts/
  │   ├── generate_relisten.py  # Fetch SoundCloud tracks → JSON
  │   └── generate-html.js      # JSON → HTML template
  ├── tests/                  # Vitest & pytest tests
  ├── package.json            # Node.js dependencies
  └── pyproject.toml          # Python deps for generator
├── .github/workflows/      # CI/CD pipelines
```

## Available Commands

| Command | Description |
|---------|-------------|
| `npm run dev` | Start Vite development server |
| `npm run build` | Build for production |
| `npm run preview` | Preview production build locally |
| `npm test` | Run Vitest tests |
| `npm run test:ui` | Run tests with UI |
| `npm run lint` | Lint code with ESLint |
| `npm run lint:fix` | Fix linting issues automatically |
| `npm run format` | Format code with Prettier |
| `npm run typecheck` | Type-check TypeScript files |
| `npm run generate:html` | Generate re-listen.html from tracks.json |

## CI/CD Pipeline

The project uses GitHub Actions for automated deployment:

1. **On push to `main`:**
   - Fetch SoundCloud tracks → `public/data/tracks.json`
   - Generate `re-listen.html` from JSON
   - Install Node.js dependencies
   - Run linting and tests
   - Build with Vite
   - Deploy to GitHub Pages

See [.github/workflows/deploy.yml](.github/workflows/deploy.yml) for details.

## Testing

### JavaScript/TypeScript Tests

```sh
cd frontend
npm test              # Run all tests
npm run test:ui       # Interactive test UI
```

### Python Tests

```sh
cd frontend
uv run pytest         # Run Python tests
```

## License

[Add your license here]

## Contact

- Instagram: [@moafunk_radio](https://www.instagram.com/moafunk_radio/)
- Telegram: [Join](https://2a5.de/h7JV)
