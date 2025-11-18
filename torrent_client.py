# torrent_client.py
import libtorrent as lt
import os

def initialize_session():
    """Initializes and configures the libtorrent session."""
    if not os.path.exists('./downloads'):
        os.makedirs('./downloads')

    settings = {
        'listen_interfaces': '0.0.0.0:6881',
        'user_agent': 'qBittorrent/4.4.2',
        'alert_mask': lt.alert.category_t.error_notification,
        'peer_connect_timeout': 15,
        'request_timeout': 20,
        'connections_limit': 1000,
        'unchoke_slots_limit': 500,
        'upload_rate_limit': 0,
        'download_rate_limit': 0,
        'active_downloads': -1,
        'active_seeds': -1,
        'active_limit': -1,
        'cache_size': 2048,
        'use_read_cache': True,
        'guided_read_cache': True,
        'suggest_mode': lt.suggest_mode_t.suggest_read_cache,
        'enable_dht': True,
        'enable_upnp': True,
        'enable_natpmp': True,
        'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881'
    }
    session = lt.session(settings)
    session.add_extension('ut_pex')
    session.add_extension('ut_metadata')
    session.add_extension('smart_ban')
    session.start_lsd()
    session.start_dht()
    session.start_upnp()
    session.start_natpmp()
    
    print("libtorrent session initialized.")
    return session