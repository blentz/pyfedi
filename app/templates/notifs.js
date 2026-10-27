
let eventSource;
let reconnectAttempts = 0;

function showUnread(numNotifs) {
    if(numNotifs > unreadNotifications) {      // unreadNotifications is set in base.html when the page loads initially.
        const outputs = document.getElementsByClassName('unreadNotificationDisplay');
        for (const el of outputs) {
            el.textContent = numNotifs;
            document.title = document.title.replace(/^\(\d+\)\s*/, ''); // remove existing (number)
            document.title = `(${numNotifs}) ${document.title}`;

            // also add 'red' class to sibling spans
            let sibling = el.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === 'SPAN') {
                    if (!sibling.classList.contains('red')) {
                        sibling.classList.add('red');
                    }
                    break;
                }
                sibling = sibling.previousElementSibling;
            }

            if (Notification.permission === "granted") {
                new Notification("{{ branding.name }}", {
                    body: `${numNotifs} notifications.`,
                    icon: "/static/images/favicon-32x32.png",
                    tag: '{{ nonce }}'
                });
            }

            if ('setAppBadge' in navigator) { // Show a number badge on pwa icon
              navigator.setAppBadge(numNotifs);
            }

        }
    }
}

function connect() {
    const userId = "{{ current_user.id }}";
    eventSource = new EventSource(`{{ notif_server }}/notifications/stream?user_id=${userId}`);

    eventSource.onmessage = (event) => {
        console.log('received message');
        const data = JSON.parse(event.data);
        console.log(data);
        // the event says only that the count changed: this storefront says what it may show (follow-up F6)
        if(data['notifs_changed']) {
            fetch('/notifications/unread_count')
                .then(function(response) {
                    if (!response.ok) throw new Error('Network response was not ok');
                    return response.json();
                })
                .then(function(counted) { showUnread(counted['num_notifs']); });
        }

        // SSE message telling the frontend that a new chat message has arrived
        if(data['conversation'] && location.href.indexOf(`chat/${data['conversation']}`)) {
            fetch(`/chat/refresh-conversation/${data['conversation']}`)
                .then(function(response) {
                    if (!response.ok) throw new Error('Network response was not ok');
                    return response.text();
                })
                .then(function(html) {
                    const conversation = document.getElementById('conversation');
                    if(conversation) {
                        stick = stickToBottom(conversation);
                        conversation.innerHTML = html;
                        if(stick) {
                            conversation.scrollTop = conversation.scrollHeight;
                        }
                    }
                });
        }

    };

    eventSource.onerror = (err) => {
        //console.error("SSE error:", err);
        eventSource.close();
        reconnectAttempts++;
        //const timeout = Math.min(30000, 1000 * 2 ** reconnectAttempts);
        const timeout = 1000;
        //console.log(`Reconnecting in ${timeout / 1000}s...`);
        if(reconnectAttempts < 1000) {
            setTimeout(connect, timeout);
        }
    };
}

connect();
