let originalTitle = document.title;
let titleInterval = null;
let notificationAudio = null;

// Инициализация звука (чтобы избежать задержки при первом воспроизведении)
function initializeAudio(src = '/static/notification.mp3') {
    if (!notificationAudio) {
        notificationAudio = new Audio(src);
        notificationAudio.load(); // Загрузить звук заранее
    }
}

// Воспроизведение звука
function playNotificationSound() {
    initializeAudio();
    if (notificationAudio) {
        const audioClone = notificationAudio.cloneNode(); // Клон для повторного проигрывания без задержек
        audioClone.play().catch(error => {
            console.log('Audio playback blocked (user interaction required):', error);
        });
    }
}

// Изменение заголовка вкладки (Title Flashing)
function startTitleFlashing(newText) {
    if (document.hasFocus() || titleInterval) {
        return; // Не мигаем, если вкладка в фокусе или уже мигает
    }
    let isNew = true;
    titleInterval = setInterval(() => {
        document.title = isNew ? newText : originalTitle;
        isNew = !isNew;
    }, 1000); // Мигать каждую секунду
}

// Отмена мигания заголовка и восстановление
function stopTitleFlashing() {
    if (titleInterval) {
        clearInterval(titleInterval);
        titleInterval = null;
        document.title = originalTitle; // Восстановить оригинальный заголовок
    }
}

// Отмена мигания при возврате фокуса
window.addEventListener('focus', () => {
    stopTitleFlashing();
});

// Начнем инициализацию, как только скрипт загрузится
initializeAudio();